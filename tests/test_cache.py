from test_http import running, client, body
from test_fetch import site, browser_pids
from aiohttp import web
import pytest


async def fetch(session, url, **controls):
    return body(await session.call_tool('fetch', {'urls': [url], **controls}))['sources'][0]


async def test_documents_reused_across_queries_and_server_restart(tmp_path):
    requests = []
    async def handler(request):
        requests.append(str(request.rel_url))
        return web.Response(content_type='text/html', text='<h1>Canonical</h1><p>' + 'Version one documentation. ' * 20 + '</p>')
    before = browser_pids()
    async with site(handler) as (base, options):
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            first = body(await session.call_tool('fetch', {'urls': [base + '?version=1'], 'query': 'one', 'content_mode': 'full_text'}))['sources'][0]
            assert 'content' in first, first
            again = body(await session.call_tool('fetch', {'urls': [base + '?version=1'], 'query': 'another', 'content_mode': 'full_text'}))['sources'][0]
            assert again['cache_state'] == 'hit'
            assert again['content_version'] == first['content_version']
            assert again['fetched_at'] == first['fetched_at']
            assert len(requests) == 1
        assert browser_pids() == before
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            saved = body(await session.call_tool('fetch', {'urls': [base + '?version=1'], 'freshness': 'cache_only'}))['sources'][0]
            assert saved['content'] == first['content']
            assert saved['fetched_at'] == first['fetched_at']
            assert len(requests) == 1


async def test_freshness_refresh_versions_and_stale_failures(tmp_path):
    version = 'One'
    requests = []
    failed = False
    async def handler(request):
        requests.append(str(request.rel_url))
        if failed:
            return web.Response(status=503)
        return web.Response(content_type='text/html', text=f'<title>{version}</title><h1>{version}</h1><p>' + 'Technical version documentation. ' * 20 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        missing = await fetch(session, base, freshness='cache_only')
        assert missing['error']['code'] == 'cache_miss'
        assert requests == []
        first = await fetch(session, base)
        version = 'Two'
        assert (await fetch(session, base))['content_version'] == first['content_version']
        aged = await fetch(session, base, max_age_seconds=0)
        assert aged['content_version'] != first['content_version']
        assert aged['cache_state'] == 'refresh'
        assert aged['fetched_at'] > first['fetched_at']
        refreshed = await fetch(session, base, freshness='refresh')
        assert refreshed['content_version'] == aged['content_version']
        assert refreshed['fetched_at'] > aged['fetched_at']
        assert refreshed['title'] == 'Two'
        fresh = await fetch(session, base, freshness='fresh_only')
        assert fresh['fetched_at'] > refreshed['fetched_at']
        different = await fetch(session, base + '?version=3', freshness='cache_only')
        assert different['error']['code'] == 'cache_miss'
        failed = True
        stale = await fetch(session, base, max_age_seconds=0)
        assert stale['cache_state'] == 'stale' and stale['stale'] is True
        assert stale['refresh_error']['code'] == 'fetch_failed'
        assert stale['fetched_at'] == fresh['fetched_at']
        assert stale['content'] == fresh['content']
        forced = await fetch(session, base, freshness='refresh')
        assert forced['cache_state'] == 'stale'
        assert forced['fetched_at'] == fresh['fetched_at']
        refused = await fetch(session, base, freshness='fresh_only')
        assert refused['error']['code'] == 'fetch_failed'
        assert 'content' not in refused and 'fetched_at' not in refused
        count = len(requests)
        only = await fetch(session, base, freshness='cache_only', max_age_seconds=0)
        assert only['stale'] is True and only['fetched_at'] == fresh['fetched_at']
        assert len(requests) == count


async def test_cap_evicts_oldest_documents_and_oversized_document_is_not_cached(tmp_path):
    async def handler(request):
        paragraphs = 100 if request.path == '/huge' else 16
        return web.Response(content_type='text/html', text=f'<h1>{request.path}</h1><p>' + 'Warning: this version must remain attributed. ' * paragraphs + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            cache_cap_bytes=2048) as (url, _), client(url) as session:
        first = await fetch(session, base + '/one')
        assert 'content' in first
        second = await fetch(session, base + '/two')
        assert 'content' in second
        assert (await fetch(session, base + '/one', freshness='cache_only'))['error']['code'] == 'cache_miss'
        assert (await fetch(session, base + '/two', freshness='cache_only'))['content'] == second['content']
        huge = await fetch(session, base + '/huge')
        assert 'content' in huge
        assert (await fetch(session, base + '/huge', freshness='cache_only'))['error']['code'] == 'cache_miss'
        assert (await fetch(session, base + '/two', freshness='cache_only'))['content'] == second['content']
        # Physical byte measurement, not a storage-side assertion of retrieval:
        # confirms the configured cap includes persisted canonical metadata.
        assert sum(p.stat().st_size for p in (tmp_path / 'cache').iterdir()) <= 2048


async def test_refreshing_same_content_does_not_evict_other_retained_sources(tmp_path):
    # Include initial-render coverage metadata in the 2560-byte cap: measured
    # rows are about 1068 bytes, so two fit but three (double-counting) do not.
    async def handler(request):
        return web.Response(content_type='text/html', text=f'<h1>{request.path}</h1><p>' + 'Stable technical documentation. ' * 15 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            cache_cap_bytes=2560) as (url, _), client(url) as session:
        first = await fetch(session, base + '/first')
        second = await fetch(session, base + '/second')
        assert 'content' in first and 'content' in second
        assert (await fetch(session, base + '/first', freshness='cache_only'))['content'] == first['content']
        refreshed = await fetch(session, base + '/second', freshness='refresh')
        assert refreshed['content_version'] == second['content_version']
        retained = await fetch(session, base + '/first', freshness='cache_only')
        assert retained['content'] == first['content']


async def test_retention_expires_documents_across_restart(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Temporary</h1><p>' + 'Canonical documentation. ' * 20 + '</p>')
    async with site(handler) as (base, options):
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            assert 'content' in await fetch(session, base)
        async with running(tmp_path, acquisition_options=options,
                cache_retention_seconds=0.01) as (url, _), client(url) as session:
            expired = await fetch(session, base, freshness='cache_only')
            assert expired['error']['code'] == 'cache_miss'


@pytest.mark.parametrize('controls', [{'freshness': 'bogus'}, {'max_age_seconds': -1},
    {'max_age_seconds': 2592001}, {'max_age_seconds': '0'}])
async def test_invalid_freshness_controls_are_rejected(tmp_path, controls):
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': ['https://example.org'], **controls})
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'
