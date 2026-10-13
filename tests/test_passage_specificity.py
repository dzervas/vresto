"""Deterministic passage quality at the public HTTP MCP seam, not live Exa parity."""
import json

from aiohttp import web
from test_fetch import site
from test_http import running, client, body, cli_response


QUERY = 'TaskGroup cancellation CancelledError uncancel'
CAVEAT = ('TaskGroup uses cancellation internally. Do not swallow CancelledError: '
          'if suppressing it is truly desired, also call uncancel to remove cancellation state.')


def documentation():
    background = ''.join(f'<p>Background topic {i}: unrelated scheduler implementation details. '
                         + 'Ordinary scheduling reference material. ' * 35 + '</p>' for i in range(30))
    return ('<h1>Asyncio reference</h1><h2>Task Object</h2>' + background +
            '<p>TaskGroup cancellation propagates to unfinished tasks.</p>' +
            '<p>' + CAVEAT + '</p>' +
            '<p>Changed in version 3.12: this warning applies to the documented release.</p>' +
            background + '<h2>Other APIs</h2><p>Storage reference.</p>')


async def test_documentation_fetch_returns_compact_qualified_canonical_passages_at_64k(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text=documentation())
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            source=cli_response(json.dumps({'data': [{'url': base, 'title': 'Asyncio', 'rank': 1}]}))) as (url, _), client(url, timeout=60) as session:
        full = body(await session.call_tool('fetch', {'urls': [base], 'response_budget_bytes': 262144}))['sources'][0]
        assert 'truncated_fields' not in full
        result = await session.call_tool('fetch', {'urls': [base], 'query': QUERY,
                                                'freshness': 'cache_only', 'response_budget_bytes': 65536})
        data = body(result)
        source = data['sources'][0]
        assert source.get('highlights'), source
        assert len(result.model_dump_json(exclude_none=True).encode()) <= 65536
        assert source['relevance'] == 'lexical_match'
        assert data['selection']['relevance_semantics'] == 'lexical_overlap_not_answer_verification'
        searched = await session.call_tool('search', {'query': QUERY, 'num_results': 1,
                                                      'response_budget_bytes': 65536})
        assert len(searched.model_dump_json(exclude_none=True).encode()) <= 65536
        assert body(searched)['results'][0]['highlights'] == source['highlights']
        qualified = [h for h in source['highlights'] if CAVEAT in h['text']]
        assert qualified
        assert any('Changed in version 3.12' in h['text'] for h in qualified)
        for highlight in source['highlights']:
            assert len(highlight['text']) < 8000
            assert highlight['text'] == full['content'][highlight['start']:highlight['end']]
            assert highlight['section'] == 'Task Object'
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [highlight['evidence_id']]}))['sources'][0]
            assert expanded['highlights'] == [highlight]


async def test_heading_poor_composed_search_is_specific_without_losing_source_diversity(tmp_path):
    # Same 64 KiB usable wire budget as the documentation fetch; no live provider
    # freshness/version claims. Short heading-poor pages must not become quotes
    # of the entire discussion just because the whole page fits the budget.
    async def handler(request):
        background = ''.join('<p>' + f'Unrelated reply {i} discusses deployment practices. ' * 4 + '</p>'
                             for i in range(3))
        return web.Response(content_type='text/html', text=(
            '<p>Discussion ' + request.path + '</p>' + background +
            '<p>For Python 3.12 only, TaskGroup cancellation propagates to unfinished tasks.</p>' +
            '<p>' + CAVEAT + '</p>' +
            '<p>Warning: this does not make cancellation a general error recovery strategy.</p>' + background))
    async with site(handler) as (base, options):
        payload = {'data': [{'url': base + '/' + name, 'title': name, 'rank': i + 1}
                            for i, name in enumerate(['discussion-a', 'discussion-b'])]}
        async with running(tmp_path, source=cli_response(json.dumps(payload)), acquisition_options=options) as (url, _), client(url, timeout=60) as session:
            result = await session.call_tool('search', {'query': QUERY, 'num_results': 2,
                                                       'response_budget_bytes': 65536})
            data = body(result)
            assert len(result.model_dump_json(exclude_none=True).encode()) <= 65536
            assert len(data['results']) == 2
            for source in data['results']:
                assert source['highlights']
                best = source['highlights'][0]
                assert len(best['text']) < 1200
                assert CAVEAT in best['text']
                assert 'Python 3.12 only' in best['text']
                assert 'does not make cancellation' in best['text']
                full = body(await session.call_tool('fetch', {'urls': [source['url']], 'freshness': 'cache_only'}))['sources'][0]
                assert full['content'][best['start']:best['end']] == best['text']
                fetched = body(await session.call_tool('fetch', {'urls': [source['url']], 'query': QUERY,
                                                                'freshness': 'cache_only', 'response_budget_bytes': 65536}))['sources'][0]
                assert fetched['highlights'] == source['highlights']
                absent = body(await session.call_tool('fetch', {'urls': [source['url']], 'query': 'xyzzymissing', 'freshness': 'cache_only'}))['sources'][0]
                assert absent['relevance'] == 'no_lexical_match' and absent['highlights'] == []


async def test_unrelated_parent_warning_does_not_scope_every_child_passage(tmp_path):
    async def handler(request):
        background = ''.join('<p>' + f'Background topic {i}. ' * 80 + '</p>' for i in range(35))
        return web.Response(content_type='text/html', text=(
            '<h1>Asyncio reference</h1><h2>Task Object</h2>' +
            '<p>General reference for task object operations.</p>' + background +
            '<p>Warning: do not use the obsolete debug operation.</p>' +
            '<h3>TaskGroup</h3><p>' + CAVEAT + '</p>' +
            '<p>Changed in version 3.12: this applies to the documented release.</p>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'query': QUERY,
                                                      'response_budget_bytes': 65536}))['sources'][0]
        assert source['highlights'], source
        quote = source['highlights'][0]
        assert quote['section'] == 'TaskGroup'
        assert CAVEAT in quote['text']
        assert 'Changed in version 3.12' in quote['text']
        assert len(quote['text']) < 1200


async def test_long_scoped_section_never_detaches_its_release_heading(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<h2>Python 3.12 only</h2><p>Reference for this release.</p>' +
            ''.join('<p>' + f'Release background {i}. ' * 20 + '</p>' for i in range(8)) +
            '<p>' + CAVEAT + '</p>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'query': QUERY}))['sources'][0]
        quote = source['highlights'][0]
        assert 'Python 3.12 only' in quote['text']
        assert CAVEAT in quote['text']
        full = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only'}))['sources'][0]
        assert full['content'][quote['start']:quote['end']] == quote['text']


async def test_window_evidence_survives_refresh_restart_and_reports_eviction(tmp_path):
    release = '3.12'
    async def handler(request):
        background = ''.join('<p>' + f'Unrelated reply {i}. ' * 30 + '</p>' for i in range(8))
        return web.Response(content_type='text/html', text=(
            '<h1>Task discussion</h1>' + background +
            '<p>For Python ' + release + ' only, TaskGroup cancellation affects sibling tasks.</p>' +
            '<p>' + CAVEAT + '</p>' + background))
    async with site(handler) as (base, options):
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
            original = body(await session.call_tool('fetch', {'urls': [base], 'query': QUERY}))['sources'][0]
            quote = original['highlights'][0]
            assert CAVEAT in quote['text'] and 'Python 3.12 only' in quote['text']
            release = '3.13'
            refreshed = body(await session.call_tool('fetch', {'urls': [base], 'query': QUERY, 'freshness': 'refresh'}))['sources'][0]
            assert refreshed['content_version'] != original['content_version']
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert expanded['highlights'] == [quote]
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert expanded['highlights'] == [quote]
            assert expanded['content_version'] == original['content_version']
        async with running(tmp_path, acquisition_options=options, cache_cap_bytes=1024) as (url, _), client(url) as session:
            evicted = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert evicted['error']['code'] == 'evidence_unavailable'
