"""OAuth resource-server verification; credentials never leave the configured AS boundary."""
import asyncio
from collections import OrderedDict
import hashlib
import hmac
import json
import math
import time
from typing import Annotated, Any
from urllib.parse import urlsplit

import aiohttp
import jwt
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl, TypeAdapter, UrlConstraints
from starlette.types import Send


def secure_url(value: str) -> str:
    """TLS everywhere except explicit loopback development endpoints."""
    if not isinstance(value, str):
        raise ValueError('OAuth URL must be a string')
    url = urlsplit(value)
    if (not url.hostname or url.username is not None or url.password is not None
            or url.query or url.fragment or (url.scheme != 'https' and not (
                url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')))):
        raise ValueError('OAuth URLs require HTTPS (HTTP is allowed on loopback only)')
    return value


class ScopeAuthMiddleware(RequireAuthMiddleware):
    """SDK 1.26 challenges omit RFC 6750's scope guidance; add it in one place."""

    async def _send_auth_error(self, send: Send, status_code: int, error: str, description: str) -> None:
        async def with_scope(message):
            if message['type'] == 'http.response.start' and self.required_scopes:
                message['headers'] = [
                    (key, value + (', scope="' + ' '.join(self.required_scopes) + '"').encode()
                     if key == b'www-authenticate' else value)
                    for key, value in message['headers']]
            await send(message)
        await super()._send_auth_error(with_scope, status_code, error, description)


class ResourceVerifier:
    """SDK TokenVerifier: bounded caches, asymmetric JWTs, then opaque validation.

    JWT validation failures never downgrade to userinfo/introspection. Userinfo
    cannot establish audience or scopes and is only an explicit legacy opt-in.
    """

    def __init__(self, oauth: AuthSettings | None, static_token: str, *, userinfo: bool = False,
                 introspection_client_id: str = '', introspection_client_secret: str = '',
                 issuer: str = '', resource: str = ''):
        self.oauth = oauth
        # AuthSettings validates SDK URLs, but AnyHttpUrl adds an empty-path slash.
        # Issuer/audience comparisons must preserve the operator's exact strings.
        self.issuer = issuer
        self.resource = resource
        self.static_token = static_token
        self.userinfo = userinfo
        self.introspection_auth = (aiohttp.BasicAuth(introspection_client_id, introspection_client_secret)
                                   if introspection_client_id else None)
        self.session: aiohttp.ClientSession | None = None
        self.metadata: dict[str, Any] = {}
        self.metadata_until = 0.0
        self.keys: list[dict[str, Any]] = []
        self.keys_until = 0.0
        self.refresh_after = 0.0
        self.cache: OrderedDict[bytes, tuple[float, AccessToken | None]] = OrderedDict()
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        self.cache.clear()

    async def _json(self, url: str, *, token: str | None = None, introspect: bool = False) -> dict[str, Any]:
        secure_url(url)
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))
        async with self.session.request(
            'POST' if introspect else 'GET', url, allow_redirects=False,
            headers={'Authorization': 'Bearer ' + token} if token and not introspect else {},
            auth=self.introspection_auth if introspect else None,
            data={'token': token, 'token_type_hint': 'access_token'} if introspect else None,
        ) as response:
            if response.status != 200:
                raise ValueError('OAuth endpoint rejected request')
            data = bytearray()
            async for chunk in response.content.iter_chunked(65_536):
                data.extend(chunk)
                if len(data) > 1_048_576:
                    raise ValueError('OAuth response exceeds limit')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError('OAuth response must be an object')
            return value

    async def _discover(self) -> dict[str, Any]:
        assert self.oauth is not None
        if time.monotonic() < self.metadata_until:
            if not self.metadata:
                raise ValueError('OAuth discovery unavailable')
            return self.metadata
        issuer = self.issuer
        parsed = urlsplit(issuer)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        urls = [origin + '/.well-known/oauth-authorization-server' + parsed.path.rstrip('/')]
        if parsed.path.rstrip('/'):
            urls.append(origin + '/.well-known/openid-configuration' + parsed.path.rstrip('/'))
        urls.append(issuer.rstrip('/') + '/.well-known/openid-configuration')
        # Failed discovery is briefly cached too, preventing per-token retries.
        self.metadata = {}
        self.metadata_until = time.monotonic() + 3
        for url in urls:
            try:
                metadata = await self._json(url)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                continue
            if metadata.get('issuer') != issuer:
                raise ValueError('OAuth discovery issuer mismatch')
            self.metadata = metadata
            self.metadata_until = time.monotonic() + 300
            return metadata
        raise ValueError('OAuth discovery unavailable')

    async def _jwks(self, metadata: dict[str, Any]) -> None:
        # Rate-limit failed fetches as well as successful forced refreshes.
        self.keys_until = time.monotonic() + 3
        document = await self._json(metadata['jwks_uri'])
        keys = document.get('keys')
        if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
            raise ValueError('Invalid JWKS')
        self.keys = keys
        self.keys_until = time.monotonic() + 300

    async def _jwt(self, token: str, metadata: dict[str, Any]) -> dict[str, Any]:
        assert self.oauth is not None
        header = jwt.get_unverified_header(token)
        algorithm = header.get('alg')
        if header.get('typ') not in ('at+jwt', 'application/at+jwt') or algorithm not in (
                'RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA'):
            raise ValueError('Unsupported access token type or algorithm')
        if time.monotonic() >= self.keys_until:
            await self._jwks(metadata)
        def matching():
            key_type = 'RSA' if algorithm.startswith(('RS', 'PS')) else 'EC' if algorithm.startswith('ES') else 'OKP'
            return [key for key in self.keys if ('kid' not in header or key.get('kid') == header['kid'])
                    and key.get('use', 'sig') == 'sig' and 'verify' in key.get('key_ops', ['verify'])
                    and key.get('alg', algorithm) == algorithm and key.get('kty') == key_type]
        candidates = matching()
        if not candidates and time.monotonic() >= self.refresh_after:
            self.refresh_after = time.monotonic() + 30
            await self._jwks(metadata)
            candidates = matching()
        for candidate in candidates:
            key = jwt.PyJWK.from_dict(candidate, algorithm=algorithm)
            try:
                return jwt.decode(token, key.key, algorithms=[algorithm],
                                  issuer=self.issuer, audience=self.resource,
                                  options={'require': ['iss', 'aud', 'exp', 'sub', 'client_id', 'iat', 'jti']})
            except jwt.InvalidSignatureError:
                continue
        raise ValueError('No matching signing key')

    def _access(self, token: str, claims: dict[str, Any], *, audience: bool) -> AccessToken:
        assert self.oauth is not None
        resource = self.resource
        if audience:
            aud = claims.get('aud')
            if aud != resource and not (isinstance(aud, list) and resource in aud):
                raise ValueError('Token audience mismatch')
            if 'iss' in claims and claims['iss'] != self.issuer:
                raise ValueError('Token issuer mismatch')
        now = time.time()
        for name in ('exp', 'nbf'):
            value = claims.get(name)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or
                    (name == 'exp' and value <= now) or (name == 'nbf' and value > now)):
                raise ValueError('Invalid token lifetime')
        # RFC 7662 permits responses without sub/client_id; JWT verification
        # already requires sub, while OIDC userinfo must always provide it.
        subject = claims.get('sub', claims.get('client_id', 'unknown') if audience else None)
        scope = claims.get('scope', '')
        if not isinstance(subject, str) or not subject or not isinstance(scope, str):
            raise ValueError('Invalid token claims')
        return AccessToken(token=token, client_id=subject, scopes=scope.split(),
                           expires_at=int(claims['exp']) if 'exp' in claims else None, resource=resource)

    async def _verify(self, token: str) -> AccessToken | None:
        metadata = await self._discover()
        if token.count('.') == 2:
            return self._access(token, await self._jwt(token, metadata), audience=True)
        if endpoint := metadata.get('introspection_endpoint'):
            claims = await self._json(endpoint, token=token, introspect=True)
            if claims.get('active') is not True:
                return None
            return self._access(token, claims, audience=True)
        if self.userinfo and (endpoint := metadata.get('userinfo_endpoint')):
            return self._access(token, await self._json(endpoint, token=token), audience=False)
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 16_384:
            return None
        result = None
        if self.oauth is not None:
            digest = hashlib.sha256(token.encode()).digest()
            async with self.lock:
                entry = self.cache.get(digest)
                if entry is not None and entry[0] > time.monotonic():
                    result = entry[1]
                else:
                    try:
                        async with asyncio.timeout(10):
                            result = await self._verify(token)
                    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, OverflowError, jwt.PyJWTError):
                        # Fixed classifications at HTTP boundary; never log token,
                        # claims, endpoint responses or exception strings.
                        result = None
                    ttl = min(30, max(0, result.expires_at - time.time())) if result and result.expires_at else 30
                    self.cache[digest] = (time.monotonic() + (ttl if result else 3), result)
                    self.cache.move_to_end(digest)
                    while len(self.cache) > 1024:
                        self.cache.popitem(last=False)
        if result is not None:
            return result
        if self.static_token and hmac.compare_digest(token.encode(), self.static_token.encode()):
            return AccessToken(token=token, client_id='static', scopes=(self.oauth.required_scopes or [])
                               if self.oauth else [])
        return None


def oauth_settings(issuer: str, resource: str, scopes: tuple[str, ...]) -> AuthSettings | None:
    if bool(issuer) != bool(resource):
        raise ValueError('OAuth issuer and resource must be configured together')
    if not issuer:
        return None
    for scope in scopes:
        if not scope or any(not (ord(c) == 0x21 or 0x23 <= ord(c) <= 0x5b or 0x5d <= ord(c) <= 0x7e) for c in scope):
            raise ValueError('Invalid OAuth scope')
    url = TypeAdapter(Annotated[AnyHttpUrl, UrlConstraints(preserve_empty_path=True)])
    return AuthSettings(issuer_url=url.validate_python(secure_url(issuer)),
                        resource_server_url=url.validate_python(secure_url(resource)), required_scopes=list(scopes))
