"""Resource-server boundary against a real local AS, without Chrome or paid calls."""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
import logging
import time

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from kagi_crawler.auth import ResourceVerifier, oauth_settings
from kagi_crawler.server import Settings, create_app
from test_http import running, client, body

RESOURCE = 'https://crawler.example/mcp'


class AuthorizationServer:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = 'first'
        self.published = [self.jwk()]
        self.hits = Counter()
        self.introspection = False
        self.introspection_response = {'active': True, 'sub': 'opaque-user', 'aud': RESOURCE, 'scope': 'read'}
        self.userinfo_response = {'sub': 'magic-user', 'email': 'public@example.org',
                                  'preferred_username': 'public', 'groups': ['users']}
        self.userinfo_status = 200
        self.discovery_issuer = None
        self.discovery_style = 'oidc'
        self.delay = 0

    def jwk(self):
        return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key())),
                'kid': self.kid, 'alg': 'RS256', 'use': 'sig'}

    def token(self, *, headers=None, **overrides):
        now = int(time.time())
        claims = {'iss': self.issuer, 'aud': RESOURCE, 'sub': 'alice', 'exp': now + 60,
                  'iat': now, 'jti': 'example', 'client_id': 'pi', 'scope': 'read'}
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm='RS256',
                          headers={'kid': self.kid, 'typ': 'at+jwt', **(headers or {})})

    async def handle(self, request):
        self.hits[request.path] += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        discovery = {
            '/.well-known/oauth-authorization-server/tenant': 'oauth',
            '/.well-known/openid-configuration/tenant': 'inserted',
            '/tenant/.well-known/openid-configuration': 'oidc',
            '/.well-known/oauth-authorization-server': 'oauth',
            '/.well-known/openid-configuration': 'oidc',
        }
        if request.path in discovery:
            if discovery[request.path] != self.discovery_style:
                return web.Response(status=404)
            data = {'issuer': self.discovery_issuer or self.issuer,
                    'jwks_uri': self.base + '/jwks', 'userinfo_endpoint': self.base + '/userinfo'}
            if self.introspection:
                data['introspection_endpoint'] = self.base + '/introspect'
            return web.json_response(data)
        if request.path == '/jwks':
            return web.json_response({'keys': self.published})
        if request.path == '/introspect':
            assert request.method == 'POST'
            assert request.headers['Authorization'] == 'Basic cnM6c2VjcmV0'
            assert (await request.post()) == {'token': 'opaque', 'token_type_hint': 'access_token'}
            return web.json_response(self.introspection_response)
        if request.path == '/userinfo':
            assert request.method == 'GET'
            assert request.headers['Authorization'] == 'Bearer opaque'
            return web.json_response(self.userinfo_response, status=self.userinfo_status)
        return web.Response(status=404)


@pytest.fixture
async def authorization_server():
    authority = AuthorizationServer()
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', authority.handle)
    async with TestServer(app) as server:
        authority.base = str(server.make_url('')).rstrip('/')
        authority.issuer = authority.base + '/tenant'
        yield authority


@asynccontextmanager
async def verifier(authority, **kwargs):
    instance = ResourceVerifier(oauth_settings(authority.issuer, RESOURCE, ('read',)), '',
                                issuer=authority.issuer, resource=RESOURCE, **kwargs)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.parametrize('style', ['oidc', 'oauth', 'inserted'])
async def test_valid_jwt_and_cached_discovery(authorization_server, style):
    authority = authorization_server
    authority.discovery_style = style
    async with verifier(authority) as verify:
        token = authority.token()
        info = await verify.verify_token(token)
        assert info.client_id == 'alice' and info.scopes == ['read'] and info.resource == RESOURCE
        assert await verify.verify_token(token) == info
        assert authority.hits['/jwks'] == 1
        assert authority.hits['/.well-known/oauth-authorization-server/tenant'] == 1


@pytest.mark.parametrize('claims,headers', [
    ({'aud': 'https://other.example/mcp'}, {}),
    ({'iss': 'https://other.example'}, {}),
    ({'exp': int(time.time()) - 100}, {}),
    ({'nbf': int(time.time()) + 3600}, {}),
    ({'sub': None}, {}),
    ({'exp': None}, {}),
    ({'iat': None}, {}),
    ({'jti': None}, {}),
    ({'client_id': None}, {}),
    ({}, {'typ': 'JWT'}),
    ({}, {'typ': 'id+jwt'}),
])
async def test_jwt_rejection_never_downgrades_to_userinfo(authorization_server, claims, headers):
    authority = authorization_server
    async with verifier(authority, userinfo=True) as verify:
        assert await verify.verify_token(authority.token(headers=headers, **claims)) is None
        assert authority.hits['/userinfo'] == 0


async def test_signature_and_algorithm_confusion_rejected(authorization_server):
    authority = authorization_server
    other = AuthorizationServer()
    other.issuer = authority.issuer
    async with verifier(authority) as verify:
        assert await verify.verify_token(other.token()) is None
        symmetric = jwt.encode({'iss': authority.issuer, 'aud': RESOURCE, 'exp': time.time() + 60},
                               'not-an-as-secret' * 4, algorithm='HS256', headers={'typ': 'at+jwt', 'kid': 'first'})
        assert await verify.verify_token(symmetric) is None


async def test_rotation_and_unknown_kid_refresh_rate_limit(authorization_server):
    authority = authorization_server
    async with verifier(authority) as verify:
        assert await verify.verify_token(authority.token()) is not None
        authority.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        authority.kid = 'second'
        authority.published.append(authority.jwk())
        assert await verify.verify_token(authority.token()) is not None
        assert authority.hits['/jwks'] == 2
        for i in range(5):
            assert await verify.verify_token(authority.token(headers={'kid': f'unknown-{i}'})) is None
        assert authority.hits['/jwks'] == 2
        verify.refresh_after = 0
        assert await verify.verify_token(authority.token(headers={'kid': 'unknown-last'})) is None
        assert authority.hits['/jwks'] == 3


@pytest.mark.parametrize('response,valid', [
    ({'active': True, 'sub': 'user', 'aud': RESOURCE}, True),
    ({'active': True, 'aud': RESOURCE}, True),
    ({'active': True, 'client_id': 'machine', 'aud': [RESOURCE]}, True),
    ({'active': False, 'sub': 'user', 'aud': RESOURCE}, False),
    ({'active': 'true', 'sub': 'user', 'aud': RESOURCE}, False),
    ({'active': True, 'sub': 'user', 'aud': 'https://other.example'}, False),
    ({'active': True, 'sub': 'user'}, False),
    ({'active': True, 'sub': 'user', 'aud': RESOURCE, 'exp': 1}, False),
    ({'active': True, 'sub': 'user', 'aud': RESOURCE, 'iss': 'https://other.example'}, False),
])
async def test_introspection_active_and_audience(authorization_server, response, valid):
    authority = authorization_server
    authority.introspection = True
    authority.introspection_response = response
    async with verifier(authority, userinfo=True, introspection_client_id='rs',
                        introspection_client_secret='secret') as verify:
        assert (await verify.verify_token('opaque') is not None) is valid
        assert authority.hits['/introspect'] == 1 and authority.hits['/userinfo'] == 0
        await verify.verify_token('opaque')
        assert authority.hits['/introspect'] == 1


@pytest.mark.parametrize('enabled,status,claims,valid', [
    (False, 200, {'sub': 'user'}, False),
    (True, 200, {'sub': 'user'}, True),
    (True, 401, {'error': 'invalid_token'}, False),
    (True, 200, {}, False),
    (True, 200, {'client_id': 'not-an-oidc-subject'}, False),
])
async def test_opaque_userinfo_explicit_opt_in(authorization_server, enabled, status, claims, valid):
    authority = authorization_server
    authority.userinfo_status = status
    authority.userinfo_response = claims
    async with verifier(authority, userinfo=enabled) as verify:
        info = await verify.verify_token('opaque')
        assert (info is not None) is valid
        if info:
            assert info.scopes == []  # OIDC claims/groups do not imply OAuth scopes.
        assert authority.hits['/userinfo'] == int(enabled)


async def test_discovery_issuer_must_match(authorization_server):
    authority = authorization_server
    authority.discovery_issuer = 'https://attacker.example'
    async with verifier(authority) as verify:
        assert await verify.verify_token(authority.token()) is None
        assert authority.hits['/jwks'] == 0


async def test_token_cache_is_bounded_and_lifetime_bound(authorization_server):
    authority = authorization_server
    async with verifier(authority) as verify:
        for i in range(1030):
            assert await verify.verify_token(f'opaque-{i}') is None
        assert len(verify.cache) == 1024
        token = authority.token(exp=int(time.time()) + 2)
        assert await verify.verify_token(token) is not None
        deadline, _ = next(reversed(verify.cache.values()))
        assert deadline - time.monotonic() <= 2
        assert all(token.encode() != key for key in verify.cache)


@pytest.fixture
def no_browser(monkeypatch):
    monkeypatch.setattr('kagi_crawler.server.Acquisition.warmup', lambda _: None)


async def test_http_challenge_metadata_scope_and_health(tmp_path, authorization_server, no_browser):
    authority = authorization_server
    async with running(tmp_path, access_token='', oauth_issuer=authority.issuer,
                       oauth_resource=RESOURCE, oauth_scopes=('read', 'write')) as (url, _):
        async with httpx.AsyncClient() as http:
            response = await http.get(url)
            assert response.status_code == 401
            challenge = response.headers['www-authenticate']
            assert 'resource_metadata="https://crawler.example/.well-known/oauth-protected-resource/mcp"' in challenge
            assert 'scope="read write"' in challenge
            metadata = await http.get(url.replace('/mcp', '/.well-known/oauth-protected-resource/mcp'))
            assert metadata.status_code == 200
            assert metadata.json() == {'resource': RESOURCE, 'authorization_servers': [authority.issuer],
                                       'scopes_supported': ['read', 'write'], 'bearer_methods_supported': ['header']}
            response = await http.get(url, headers={'Authorization': 'Bearer ' + authority.token()})
            assert response.status_code == 403
            assert 'error="insufficient_scope"' in response.headers['www-authenticate']
            assert 'scope="read write"' in response.headers['www-authenticate']
            health_url = url.replace('/mcp', '/health')
            assert (await http.get(health_url)).json() == {'status': 'ok'}
            missing = await http.get(health_url, headers={'Authorization': 'Bearer ' + authority.token()})
            assert missing.json() == {'status': 'ok'}
            detail = await http.get(health_url, headers={'Authorization': 'Bearer ' + authority.token(scope='read write')})
            assert detail.json()['browser'] == 'stopped'
            assert 'acquisition' in detail.json()


@pytest.mark.parametrize('oauth,static,unauthenticated,provided,accepted', [
    (True, 'fallback', False, 'oauth', True),
    (True, 'fallback', False, 'fallback', True),
    (True, '', False, 'oauth', True),
    (True, '', False, 'wrong', False),
    (False, 'fallback', False, 'fallback', True),
    (False, 'fallback', False, 'wrong', False),
    (False, '', True, '', True),
    (True, 'fallback', True, 'wrong', True),
])
async def test_http_mode_matrix(tmp_path, authorization_server, no_browser,
                                oauth, static, unauthenticated, provided, accepted):
    authority = authorization_server
    token = authority.token() if provided == 'oauth' else provided
    async with running(tmp_path, access_token=static, unauthenticated=unauthenticated,
                       oauth_issuer=authority.issuer if oauth else '',
                       oauth_resource=RESOURCE if oauth else '', oauth_scopes=('read',) if oauth else ()) as (url, _):
        async with httpx.AsyncClient() as http:
            response = await http.get(url.replace('/mcp', '/protected'),
                                      headers={'Authorization': 'Bearer ' + token} if token else {})
            assert response.status_code == (404 if accepted else 401)


@pytest.mark.parametrize('settings', [
    {}, {'oauth_issuer': 'https://issuer.example'}, {'oauth_resource': RESOURCE},
    {'oauth_issuer': 'http://issuer.example', 'oauth_resource': RESOURCE},
    {'oauth_issuer': 'https://issuer.example#bad', 'oauth_resource': RESOURCE},
    {'oauth_issuer': 'https://issuer.example', 'oauth_resource': RESOURCE, 'oauth_scopes': ('bad"scope',)},
])
def test_missing_or_invalid_auth_refuses_startup(settings):
    with pytest.raises(ValueError):
        create_app(Settings(access_token='', session_token='upstream', **settings))


async def test_tool_logs_identity_without_tokens(tmp_path, authorization_server, no_browser, caplog):
    authority = authorization_server
    caplog.set_level(logging.INFO)
    token = authority.token(sub='alice\nforged=log')
    async with running(tmp_path, access_token='', oauth_issuer=authority.issuer, oauth_resource=RESOURCE) as (url, _):
        async with client(url, token=token) as session:
            assert not (await session.call_tool('search', {'query': 'public', 'content_mode': 'discovery'})).isError
    assert 'sub="alice\\nforged=log"' in caplog.text
    assert token not in caplog.text and 'subscriber-secret' not in caplog.text


async def test_upstream_secret_redacted_without_static_token(tmp_path, no_browser):
    async with running(tmp_path, access_token='', unauthenticated=True) as (url, _):
        async with client(url, token='') as session:
            response = await session.call_tool('search', {'query': 'subscriber-secret'})
            assert body(response)['error']['code'] == 'invalid_input'


async def test_root_issuer_preserves_exact_discovery_and_metadata(tmp_path, authorization_server, no_browser):
    authority = authorization_server
    authority.issuer = authority.base  # No path and, importantly, no added slash.
    async with verifier(authority) as verify:
        assert await verify.verify_token(authority.token()) is not None
    async with running(tmp_path, access_token='', oauth_issuer=authority.issuer, oauth_resource=RESOURCE) as (url, _):
        async with httpx.AsyncClient() as http:
            metadata = await http.get(url.replace('/mcp', '/.well-known/oauth-protected-resource/mcp'))
            assert metadata.json()['authorization_servers'] == [authority.issuer]
            assert 'scopes_supported' not in metadata.json()


async def test_jwt_kid_is_optional_with_multiple_keys(authorization_server):
    authority = authorization_server
    other = AuthorizationServer()
    authority.published.insert(0, other.jwk())
    claims = jwt.decode(authority.token(), options={'verify_signature': False})
    token = jwt.encode(claims, authority.key, algorithm='RS256', headers={'typ': 'at+jwt'})
    async with verifier(authority) as verify:
        assert await verify.verify_token(token) is not None


async def test_auth_timeouts_fail_closed_and_cache_discovery_failure(authorization_server):
    authority = authorization_server
    authority.delay = .05
    async with verifier(authority) as verify:
        # Exercise aiohttp's real timeout with a small injected transport bound.
        verify.session = ClientSession(timeout=ClientTimeout(total=.01))
        async with asyncio.timeout(.5):
            assert await verify.verify_token(authority.token()) is None
        hits = sum(authority.hits.values())
        assert await verify.verify_token(authority.token(jti='different')) is None
        assert sum(authority.hits.values()) == hits


async def test_invalid_bearer_never_logged(tmp_path, authorization_server, no_browser, caplog):
    authority = authorization_server
    caplog.set_level(logging.INFO)
    token = 'sensitive-invalid-bearer'
    async with running(tmp_path, access_token='', oauth_issuer=authority.issuer, oauth_resource=RESOURCE) as (url, _):
        async with httpx.AsyncClient() as http:
            response = await http.get(url, headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == 401
            health = await http.get(url.replace('/mcp', '/health'), headers={'Authorization': 'Bearer ' + token})
            assert health.json() == {'status': 'ok'}
    assert token not in caplog.text
