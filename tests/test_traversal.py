from test_http import running, client, body
from test_fetch import site, browser_pids
from aiohttp import web
import pytest
import asyncio
from dataclasses import replace


async def test_related_pages_are_opt_in_scoped_and_cached(tmp_path):
    from retrieval_helpers import running, client
    seen = []
    async def handler(request):
        seen.append(request.path)
        links = {'/docs': ['/docs/a', '/docs/a#cycle', '/outside'],
                 '/docs/a': ['/docs', '/docs/b'], '/docs/b': []}
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>' +
            'Warning: version 2 only. Public technical documentation describes supported behavior. ' * 20 + '</p>' +
            ''.join(f'<a href="{p}">Related</a>' for p in links.get(request.path, [])))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        args = {'urls': [base + '/docs'], 'traversal': {'origin': base, 'path_prefix': '/docs'}}
        first = body(await session.call_tool('fetch', args))
        assert [s['requested_url'] for s in first['sources']] == [base + p for p in ['/docs', '/docs/a']]
        assert first['traversal']['omissions'].get('depth_limit', 0) >= 1, first
        assert first['traversal']['omissions']['scope_rejected'] >= 1
        second = body(await session.call_tool('fetch', args))
        assert all(s['cache_state'] == 'hit' for s in second['sources'])
        assert seen == ['/docs', '/docs/a']
        assert all('link_targets' not in s for s in second['sources'])


async def test_exact_origin_escape_is_excluded_after_native_redirect_contact(tmp_path):
    from retrieval_helpers import running, client
    forbidden = []
    async def sink(request):
        forbidden.append(request.path)
        return web.Response(content_type='text/html', text='<h1>Wrong origin</h1>')
    async with site(sink) as (other, other_options):
        async def handler(request):
            if request.path == '/docs/redirect':
                raise web.HTTPFound(other + '/docs/escaped')
            return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation with useful details.</p>' +
                f'<a href="{other}/docs/link">External</a><a href="/docs/redirect">Redirect</a>')
        async with site(handler) as (base, options):
            options = replace(options, fixture_destinations=options.fixture_destinations + other_options.fixture_destinations)
            async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
                data = body(await session.call_tool('fetch', {'urls': [base + '/docs'],
                    'traversal': {'origin': base, 'path_prefix': '/docs'}}))
                assert len(data['sources']) == 2
                assert data['sources'][1]['error']['code'] == 'scope_rejected'
                assert data['traversal']['omissions']['scope_rejected'] == 2
                assert forbidden == ['/docs/escaped']
                assert data['traversal']['network_scope_enforced'] is False


@pytest.mark.parametrize('redirect', ['/outside', '/docs-other', '/docs/%2e%2e/outside'])
async def test_traversal_excludes_out_of_scope_redirect_after_contact(tmp_path, redirect):
    from retrieval_helpers import running, client
    seen = []
    async def handler(request):
        seen.append(request.path)
        if request.path == '/docs/seed':
            raise web.HTTPFound(redirect)
        return web.Response(content_type='text/html', text='<h1>Escaped</h1>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base + '/docs/seed'],
            'traversal': {'origin': base, 'path_prefix': '/docs'}}))
        assert data['sources'][0]['error']['code'] == 'scope_rejected'
        assert data['traversal']['omissions']['scope_rejected'] == 1
        assert seen[0] == '/docs/seed' and len(seen) == 2
        cached = body(await session.call_tool('fetch', {'urls': [base + '/docs/seed'], 'freshness': 'cache_only'}))
        assert cached['sources'][0]['error']['code'] == 'cache_miss'


async def test_failed_pages_count_toward_attempt_limit_and_share_highlight_budget(tmp_path):
    from retrieval_helpers import running, client
    seen = []
    async def handler(request):
        seen.append(request.path)
        if request.path != '/docs':
            return web.Response(status=404)
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Warning: version 2 only.</p>' +
            ''.join(f'<a href="/docs/{i}">Related</a>' for i in range(10)))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base + '/docs'], 'query': 'version',
            'traversal': {'origin': base, 'path_prefix': '/docs', 'max_pages': 3, 'max_depth': 3}}))
        assert len(data['sources']) == 3
        assert len(seen) == 3
        assert data['sources'][0]['highlights']
        assert all(s['error']['code'] == 'fetch_failed' for s in data['sources'][1:])
        assert data['traversal']['omissions'].get('page_limit', 0) > 0, data


async def test_allowed_redirect_alias_is_not_reacquired_as_a_cycle(tmp_path):
    from retrieval_helpers import running, client
    seen = []
    async def handler(request):
        seen.append(request.path)
        if request.path == '/docs/seed':
            raise web.HTTPFound('/docs/final')
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation with useful details.</p><a href="/docs/final">Self</a><a href="/docs/seed">Alias</a>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base + '/docs/seed'],
            'traversal': {'origin': base, 'path_prefix': '/docs', 'max_depth': 3}}))
        assert len(data['sources']) == 1
        assert data['traversal']['attempted_pages'] == 1
        assert seen == ['/docs/seed', '/docs/final']


async def test_traversal_document_scope_does_not_intercept_subrequests(tmp_path):
    from retrieval_helpers import running, client
    forbidden = []
    async def sink(request):
        forbidden.append(request.path)
        return web.Response(text='Private')
    async with site(sink) as (private, _):
        async def handler(request):
            return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation with useful details.</p>' +
                f'<script>fetch("{private}/private").catch(()=>{{}})</script>')
        async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            data = body(await session.call_tool('fetch', {'urls': [base + '/docs'],
                'traversal': {'origin': base, 'path_prefix': '/docs'}}))
            assert 'Guide' in data['sources'][0]['content']
            assert forbidden == ['/private']


async def test_zero_depth_and_cached_redirect_never_widen_path_scope(tmp_path):
    from retrieval_helpers import running, client
    seen = []
    async def handler(request):
        seen.append(request.path)
        if request.path == '/docs/redirect':
            raise web.HTTPFound('/outside')
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation with useful details.</p><a href="/docs/child">Next</a>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        args = {'urls': [base + '/docs'], 'traversal': {'origin': base, 'path_prefix': '/docs', 'max_depth': 0}}
        data = body(await session.call_tool('fetch', args))
        assert len(data['sources']) == 1
        assert data['traversal']['omissions']['depth_limit'] == 1
        await session.call_tool('fetch', {'urls': [base + '/docs/redirect']})
        before = list(seen)
        args['urls'] = [base + '/docs/redirect']
        data = body(await session.call_tool('fetch', args))
        assert data['sources'][0]['error']['code'] == 'scope_rejected'
        assert seen == before


async def test_frontier_and_total_bytes_are_disclosed(tmp_path):
    from retrieval_helpers import running, client
    seen = []
    async def handler(request):
        seen.append(request.path)
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>' + 'documentation ' * 80 + '</p>' +
            ''.join(f'<a href="/docs/{i}">Related</a>' for i in range(5)))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        args = {'urls': [base + '/docs'], 'traversal': {'origin': base, 'path_prefix': '/docs', 'max_frontier': 1}}
        data = body(await session.call_tool('fetch', args))
        assert len(data['sources']) == 2
        assert data['traversal']['omissions']['frontier_limit'] > 0
        args['traversal']['max_bytes'] = 1024
        data = body(await session.call_tool('fetch', args))
        assert data['sources'][0]['error']['code'] == 'traversal_byte_limit'
        assert data['traversal']['omissions']['byte_limit'] > 0
        assert len(seen) == 2  # Cached seed cannot evade traversal byte bounds.


async def test_deadline_keeps_partial_sources_and_stops_next_page(tmp_path):
    from retrieval_helpers import running, client
    seen, release = [], asyncio.Event()
    async def handler(request):
        seen.append(request.path)
        if request.path == '/docs/slow':
            await release.wait()
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation with version constraints and useful guidance.</p><a href="/docs/slow">Next</a><a href="/docs/after">Later</a>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        await session.call_tool('fetch', {'urls': [base + '/docs']})
        # Allow native context/page setup, then hold HTTP until the original
        # traversal deadline expires; rendering speed must not release the page.
        try:
            data = body(await asyncio.wait_for(session.call_tool('fetch', {'urls': [base + '/docs'],
                'traversal': {'origin': base, 'path_prefix': '/docs', 'max_seconds': 2}}), 4))
            assert [s['requested_url'] for s in data['sources']] == [base + '/docs', base + '/docs/slow']
            assert data['sources'][0]['cache_state'] == 'hit'
            assert data['sources'][1]['error']['code'] == 'fetch_timeout'
            assert data['traversal']['omissions']['time_limit'] == 1
            assert seen == ['/docs', '/docs/slow']
        finally:
            release.set()


async def test_traversal_preserves_meaningful_query_and_blank_parameters(tmp_path):
    from retrieval_helpers import running, client
    seen = []
    async def handler(request):
        seen.append(request.raw_path)
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation with version constraints and useful guidance.</p><a href="/docs/page?ref=v2&amp;empty=">Version</a>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('crawl', {'url': base + '/docs', 'path_prefix': '/docs'}))
        assert seen == ['/docs', '/docs/page?ref=v2&empty=']
        assert data['sources'][1]['requested_url'].endswith('?ref=v2&empty=')


async def test_traversal_queue_pressure_uses_existing_shared_permit(tmp_path):
    from retrieval_helpers import running, client
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return web.Response(status=404)
    async with site(handler) as (base, options):
        options = replace(options, deadline=2)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as first, client(url) as second:
            args = {'urls': [base + '/docs'], 'traversal': {'origin': base, 'path_prefix': '/docs'}}
            active = asyncio.create_task(first.call_tool('fetch', args))
            await asyncio.wait_for(entered.wait(), 3)
            queued = asyncio.create_task(second.call_tool('fetch', args))
            await asyncio.sleep(0.03)
            excess = body(await second.call_tool('fetch', args))
            assert excess['sources'][0]['error']['code'] == 'fetch_timeout'
            assert body(await queued)['sources'][0]['error']['code'] == 'fetch_timeout'
            release.set()
            assert body(await active)['sources'][0]['error']['code'] == 'fetch_timeout'


async def test_cancelled_traversal_releases_tab_and_retains_cached_seed(tmp_path):
    from mcp import types
    entered, disconnected = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.path == '/docs/slow':
            entered.set()
            r = web.StreamResponse(headers={'Content-Type': 'text/html'})
            await r.prepare(request)
            try:
                while True:
                    await r.write(b'x')
                    await asyncio.sleep(0.02)
            except ConnectionError:
                disconnected.set()
            return r
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation with version constraints and useful guidance.</p><a href="/docs/slow">Next</a>')
    before, calls = browser_pids(), []
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, calls=calls) as session:
        await session.call_tool('fetch', {'urls': [base + '/docs']})
        task = asyncio.create_task(session.call_tool('crawl', {'url': base + '/docs', 'path_prefix': '/docs'}))
        await asyncio.wait_for(entered.wait(), 5)
        await session.send_notification(types.ClientNotification(types.CancelledNotification(
            method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=calls[-1]))))
        try:
            await task
        except Exception as exc:
            assert 'cancel' in str(exc).lower()
        await asyncio.wait_for(disconnected.wait(), 5)
        assert len(browser_pids() - before) == 1
        cached = body(await session.call_tool('fetch', {'urls': [base + '/docs'], 'freshness': 'ordinary'}))
        assert cached['sources'][0]['cache_state'] == 'hit'
    assert browser_pids() == before


@pytest.mark.parametrize('include_urls,traversal', [(False, None), (False, 'scope'), (True, 'scope')])
async def test_evidence_expansion_rejects_traversal_before_visiting_pages(tmp_path, include_urls, traversal):
    seen = []
    async def handler(request):
        seen.append(request.path)
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Version 2 requires connect_safe. Public technical documentation preserves safety requirements.</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base + '/docs'], 'query': 'connect_safe'}))['sources'][0]
        quote = source['highlights'][0]
        args = {'evidence_ids': [quote['evidence_id']], 'traversal':
            {'origin': base, 'path_prefix': '/docs'} if traversal else None}
        if include_urls:
            args['urls'] = [base + '/docs/not-contacted']
        result = await session.call_tool('expand', args)
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'
        expanded = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]}))
        assert expanded['content_mode'] == 'evidence_expansion'
        assert expanded['sources'][0]['highlights'] == [quote]
        assert 'link_targets' not in expanded['sources'][0]
        assert seen == ['/docs']


async def test_traversed_pdf_keeps_page_references_and_expands_same_version(tmp_path):
    from retrieval_helpers import running, client
    from test_pdf import pdf_bytes
    seen = []
    async def handler(request):
        seen.append(request.path)
        if request.path == '/docs/paper':
            return web.Response(content_type='application/pdf', body=pdf_bytes([
                'Version 2 connect_safe requires TLS. Never disable verification.',
                'Compatibility: legacy mode is not supported.']))
        return web.Response(content_type='text/html', text='<h1>Guide</h1><p>Public technical documentation describes supported behavior and safety constraints.</p><a href="/docs/paper">Paper</a>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        args = {'urls': [base + '/docs'], 'query': 'connect_safe',
            'traversal': {'origin': base, 'path_prefix': '/docs'}}
        data = body(await session.call_tool('fetch', args))
        source = data['sources'][1]
        assert source['document_type'] == 'pdf'
        quote = source['highlights'][0]
        assert quote['pages'] == [1, 2]
        assert 'legacy mode is not supported.' in quote['text']
        expanded = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']], 'response_budget_bytes': 4096}))
        assert expanded['sources'][0]['highlights'] == [quote]
        assert expanded['sources'][0]['pdf'] == source['pdf']
        assert expanded['sources'][0]['content_version'] == source['content_version']
        args['freshness'] = 'cache_only'
        cached = body(await session.call_tool('fetch', args))
        assert all(s['cache_state'] == 'hit' for s in cached['sources'])
        assert cached['sources'][1]['highlights'][0] == quote
        assert seen == ['/docs', '/docs/paper']


@pytest.mark.parametrize('traversal', [{'path_prefix': '/docs'}, {'origin': 'https://example.org'},
    {'origin': 'https://example.org', 'path_prefix': '/docs', 'max_pages': 21},
    {'origin': 'https://example.org/path', 'path_prefix': '/docs'},
    {'origin': 'https://example.org', 'path_prefix': '//docs'},
    {'origin': 'https://example.org', 'path_prefix': '/docs', 'max_depth': 4}])
def test_internal_traversal_rejects_invalid_bounds_and_requires_explicit_scope(traversal):
    from pydantic import ValidationError
    from vresto.contract import FetchRequest

    with pytest.raises(ValidationError):
        FetchRequest(urls=['https://example.org/docs'], traversal=traversal)
