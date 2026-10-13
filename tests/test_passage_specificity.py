"""Deterministic passage quality at the public HTTP MCP seam, not live Exa parity."""
import json

import pytest

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


@pytest.mark.parametrize('heading', ['Python 3.12 only', '<a href="https://docs.python.org/3.12/release" title="Release scope">Python 3.12 only</a>'])
async def test_long_scoped_section_never_detaches_its_release_heading(tmp_path, heading):
    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<h2>' + heading + '</h2><p>Reference for this release.</p>' +
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


async def test_linked_documentation_headings_do_not_turn_url_versions_into_release_scopes(tmp_path):
    async def handler(request):
        html = documentation().replace('Asyncio reference</h1>',
            '<a title="Link to this heading" href="https://docs.python.org/3.12/library/asyncio-task.html">Asyncio reference</a></h1>')
        html = html.replace('Task Object</h2>',
            '<a title="Link to this heading" href="https://docs.python.org/3.12/library/asyncio-task.html#task-object">Task Object</a></h2>')
        return web.Response(content_type='text/html', text=html)
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            source=cli_response(json.dumps({'data': [{'url': base, 'title': 'Asyncio', 'rank': 1}]}))) as (url, _), client(url, timeout=60) as session:
        full = body(await session.call_tool('fetch', {'urls': [base], 'response_budget_bytes': 262144}))['sources'][0]
        for tool, arguments, key in [('fetch', {'urls': [base], 'freshness': 'cache_only'}, 'sources'),
                                     ('search', {'num_results': 1}, 'results')]:
            result = await session.call_tool(tool, {**arguments, 'query': QUERY, 'response_budget_bytes': 65536})
            source = body(result)[key][0]
            assert source['highlights'], source
            assert any(CAVEAT in h['text'] for h in source['highlights'])
            for h in source['highlights']:
                assert len(h['text']) < 8000
                assert 'Task Object' in h['section']
                assert h['text'] == full['content'][h['start']:h['end']]


@pytest.mark.parametrize('heading', ['Warning', '<a href="https://docs.python.org/3.12/warning" title="Link to this heading">Warning</a>'])
async def test_every_citable_claim_keeps_its_attached_warning(tmp_path, heading):
    claim = 'TaskGroup cancellation propagates CancelledError to unfinished tasks.'
    warning = 'Do not swallow CancelledError; if suppressing it, also call uncancel.'
    async def handler(request):
        background = ''.join('<p>' + f'Background topic {i}. ' * 30 + '</p>' for i in range(12))
        return web.Response(content_type='text/html', text=(
            '<h1>Task reference</h1>' + background + '<p>' + claim + ' Sibling tasks receive the cancellation signal.' * 12 + '</p>' +
            '<h2>' + heading + '</h2><p>' + 'Important qualification for the preceding claim. ' * 30 + '</p><p>' + warning + '</p><p>' +
            'This qualification applies to the preceding cancellation claim. ' * 8 + '</p>' +
            '<h2>Other APIs</h2><p>Unrelated reference.</p>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            source=cli_response(json.dumps({'data': [{'url': base, 'title': 'Tasks', 'rank': 1}]}))) as (url, _), client(url, timeout=60) as session:
        for tool, arguments, key in [('fetch', {'urls': [base]}, 'sources'), ('search', {'num_results': 1}, 'results')]:
            source = body(await session.call_tool(tool, {**arguments, 'query': QUERY}))[key][0]
            claims = [h for h in source['highlights'] if claim in h['text']]
            assert claims
            assert all(warning in h['text'] for h in claims)
            full = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only'}))['sources'][0]
            for h in claims:
                assert len(h['text']) < 5000
                assert h['text'] == full['content'][h['start']:h['end']]


async def test_shorter_fence_marker_cannot_expose_a_code_heading_as_a_section(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<h1>Code reference</h1><p>Example follows.</p><p>````python<br>'
            'sample = 1<br>```<br># Misleading section<br>fence_probe = 2<br>````<br></p>'
            '<h1>Other APIs</h1><p>Unrelated reference.</p>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        full = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert '````python\nsample = 1\n```\n# Misleading section' in full['content'].replace('  \n', '\n')
        selected = body(await session.call_tool('fetch', {'urls': [base], 'query': 'fence_probe', 'freshness': 'cache_only'}))['sources'][0]
        assert selected['highlights']
        for h in selected['highlights']:
            assert h['section'] == 'Code reference'
            assert '````python' in h['text']
            assert h['text'] == full['content'][h['start']:h['end']]


async def test_scoped_reference_windows_do_not_crowd_out_compact_cancellation_caveat(tmp_path):
    qualified_caveat = CAVEAT.replace('TaskGroup', 'asyncio.TaskGroup').replace('CancelledError', 'asyncio.CancelledError')
    async def handler(request):
        reference = ''.join('<p>' + f'Operation {i}: cancellation raises CancelledError; uncancel handles state. ' +
                            'Ordinary reference details. ' * 16 + '</p>' for i in range(30))
        return web.Response(content_type='text/html', text=(
            '<h1>API reference</h1><h2>Task Cancellation</h2><p>' +
            'Cancellation reference details. ' * 50 + '</p><p>' + qualified_caveat + '</p>' +
            '<h2>Task Object</h2><p>This object is not thread-safe.</p>' + reference +
            '<p>TaskGroup uses these operations internally.</p>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            source=cli_response(json.dumps({'data': [{'url': base, 'title': 'Tasks', 'rank': 1}]}))) as (url, _), client(url, timeout=60) as session:
        for tool, arguments, key in [('fetch', {'urls': [base]}, 'sources'), ('search', {'num_results': 1}, 'results')]:
            source = body(await session.call_tool(tool, {**arguments, 'query': QUERY, 'response_budget_bytes': 65536}))[key][0]
            assert any(qualified_caveat in h['text'] for h in source['highlights'])
            full = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only'}))['sources'][0]
            for h in source['highlights']:
                assert h['text'] == full['content'][h['start']:h['end']]
                if h['section'] == 'Task Object':
                    assert 'not thread-safe' in h['text']


async def test_bare_identifier_query_matches_qualified_names_without_dropping_qualification(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<h1>Cancellation</h1><p>asyncio.TaskGroup uses asyncio.CancelledError for cancellation.</p>'
            '<p>Do not swallow asyncio.CancelledError; also call Task.uncancel when suppressing it.</p>'
            '<h1>Other APIs</h1><p>other.TaskGroup performs unrelated storage operations.</p>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'query': 'CancelledError'}))['sources'][0]
        assert source['highlights']
        assert all('Do not swallow asyncio.CancelledError' in h['text'] for h in source['highlights'])
        qualified = body(await session.call_tool('fetch', {'urls': [base], 'query': 'asyncio.TaskGroup', 'freshness': 'cache_only'}))['sources'][0]
        assert qualified['highlights']
        assert all('other.TaskGroup' not in h['text'] for h in qualified['highlights'])


@pytest.mark.parametrize('explanation', ['Cleanup scheduling details. ', 'Cleanup scheduling details '])
async def test_attached_warning_qualifies_claims_across_intervening_explanation(tmp_path, explanation):
    # Exact original reviewer fixture prose and query: a cleanup explanation
    # separates the propagation claim from its attached Warning heading.
    claim = 'TaskGroup cancellation propagates to unfinished tasks.'
    caveat = 'Do not swallow CancelledError; if suppression is desired also call uncancel.'
    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<h1>TaskGroup</h1>' + ''.join('<p>' + 'Background scheduling. ' * 30 + '</p>' for _ in range(4)) +
            '<p>' + claim + ' ' + 'TaskGroup cancellation mechanics. ' * 20 + '</p>' +
            '<p>' + explanation * 30 + '</p><h2>Warning</h2><p>' + caveat + '</p>' +
            '<p>' + 'Other scheduling details. ' * 30 + '</p>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            source=cli_response(json.dumps({'data': [{'url': base, 'title': 'Tasks', 'rank': 1}]}))) as (url, _), client(url, timeout=60) as session:
        full = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        for tool, arguments, key in [('fetch', {'urls': [base], 'freshness': 'cache_only'}, 'sources'),
                                     ('search', {'num_results': 1}, 'results')]:
            source = body(await session.call_tool(tool, {**arguments, 'query': 'TaskGroup cancellation'}))[key][0]
            claims = [h for h in source['highlights'] if claim in h['text']]
            assert claims
            assert all(caveat in h['text'] for h in claims)
            for h in claims:
                assert h['section'] == 'TaskGroup'
                assert h['text'] == full['content'][h['start']:h['end']]
                expanded = body(await session.call_tool('fetch', {'evidence_ids': [h['evidence_id']]}))['sources'][0]
                assert expanded['highlights'] == [h]
        small = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only',
            'query': 'TaskGroup cancellation', 'response_budget_bytes': 4096}))
        assert small['budget']['partial_coverage']
        assert all(not source.get('highlights') for source in small['sources'])
