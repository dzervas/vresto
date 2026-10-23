import base64
import hashlib
import json

import pytest
from aiohttp import web
from test_fetch import site
from test_http import running, client, body, cli_response


async def test_explicit_highlights_search_acquires_ten_and_returns_verbatim_qualified_evidence(tmp_path):
    visited = []
    async def handler(request):
        visited.append(request.path)
        if request.path == '/10':
            return web.Response(status=404)
        return web.Response(content_type='text/html', text=f'''<title>Guide {request.path}</title>
        <h1>Client {request.path}</h1><h2>connect_safe version 2.4</h2>
        <p>connect_safe supports retries in version 2.4 only.</p>
        <p>Warning: do not enable retries on version 2.3; it is not safe.</p>
        <pre><code>connect_safe(retries=0)</code></pre>
        <table><tr><th>Version</th><th>Retries</th></tr><tr><td>2.4</td><td>0</td></tr></table>
        <h2>Other topic</h2><p>Unrelated storage information.</p>''')
    async with site(handler) as (base, options):
        payload = {'data': [{'t': 0, 'rank': i, 'url': f'{base}/{i}', 'title': f'Guide {i}',
                             'snippet': 'Not acquired evidence'} for i in range(1, 13)]}
        async with running(tmp_path, source=cli_response(json.dumps(payload)), acquisition_options=options) as (url, _), client(url, timeout=60) as session:
            result = body(await session.call_tool('search', {'query': 'connect_safe 2.4 retries', 'content_mode': 'highlights', 'quick_answer': False}))
            assert result['content_mode'] == 'highlights'
            assert len(result['results']) == 10
            assert set(visited) == {f'/{i}' for i in range(1, 11)}
            assert result['results'][-1]['error']['code'] == 'fetch_failed'
            for source in result['results'][:-1]:
                assert 'content' not in source
                assert 'error' not in source, source
                assert source['highlights']
                full = body(await session.call_tool('fetch', {'urls': [source['url']], 'freshness': 'ordinary'}))['sources'][0]
                assert full['content_version'] == source['content_version']
                assert hashlib.sha256(full['content'].encode()).hexdigest() == source['content_version']
                highlight = source['highlights'][0]
                assert highlight['text'] in full['content']
                assert full['content'][highlight['start']:highlight['end']] == highlight['text']
                assert 'version 2.3' in highlight['text'] and 'not safe' in highlight['text']
                assert 'connect_safe(retries=0)' in highlight['text'] and '|' in highlight['text']
                assert highlight['section'] == 'connect_safe version 2.4'
                assert highlight['evidence_id']
            assert result['budget']['partial_coverage']


async def test_query_selection_reuses_canonical_versions_and_discloses_absence(tmp_path):
    visits = []
    version = '2.4'
    async def handler(request):
        visits.append(request.path)
        return web.Response(content_type='text/html', text=f'''<h1>Technical manual</h1>
        <h2>connect_safe</h2><p>connect_safe works in {version} only.</p>
        <h3>Warning</h3><p>Do not use it on version 2.3.</p>
        <h2>store_atomic</h2><p>store_atomic writes durable storage.</p>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        first = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
        assert first['highlights'][0]['section'] == 'connect_safe'
        assert 'Do not use it on version 2.3.' in first['highlights'][0]['text']
        second = body(await session.call_tool('fetch', {'urls': [base], 'query': 'store_atomic'}))['sources'][0]
        assert second['highlights'][0]['section'] == 'store_atomic'
        assert first['content_version'] == second['content_version']
        assert second['cache_state'] == 'hit'
        assert visits == ['/']
        absent = body(await session.call_tool('fetch', {'urls': [base], 'query': 'xyzzymissing'}))['sources'][0]
        assert absent['relevance'] == 'no_lexical_match' and absent['highlights'] == []
        full = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe', 'content_mode': 'full_text'}))['sources'][0]
        assert 'store_atomic' in full['content'] and 'highlights' not in full
        old_id = first['highlights'][0]['evidence_id']
        payload = json.loads(base64.urlsafe_b64decode(old_id[3:] + '=' * (-len(old_id[3:]) % 4)))
        assert payload[:2] == [base, first['content_version']]
        assert full['content'][payload[2]:payload[3]] == first['highlights'][0]['text']
        assert hashlib.sha256(first['highlights'][0]['text'].encode()).hexdigest() == payload[4]
        version = '2.5'
        changed = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe', 'freshness': 'fresh_only'}))['sources'][0]
        assert changed['content_version'] != first['content_version']
        assert changed['highlights'][0]['evidence_id'] != old_id


@pytest.mark.parametrize('heading', ['v2.4', '2.4', '2.4.1-rc.2', 'v2026.10'])
async def test_numeric_version_ancestry_survives_selection_ids_and_atomic_budget(tmp_path, heading):
    from retrieval_helpers import running, client
    async def handler(request):
        return web.Response(content_type='text/html', text=f'''<h1>{heading}</h1>
        <p>Release reference for client behavior.</p>
        <h2>Linux only</h2><p>Never use this interface on unsupported platforms.</p>
        <h3>Background</h3><p>{'Detailed background context. ' * 100}</p>
        <h3>connect_safe</h3><p>connect_safe enables transactions.</p>
        <h4>Warning</h4><p>Do not disable validation.</p>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        full = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        selected = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
        quote = selected['highlights'][0]
        assert f'# {heading}' in quote['text']
        assert 'Linux only' in quote['text'] and 'Never use' in quote['text']
        assert 'Do not disable validation.' in quote['text']
        assert quote['text'] == full['content'][quote['start']:quote['end']]
        expanded = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
        assert expanded['highlights'][0]['text'] == quote['text']
        small = await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe', 'response_budget_bytes': 4096})
        data = body(small)
        assert len(small.model_dump_json(exclude_none=True).encode()) <= 4096
        assert data['budget']['partial_coverage']
        assert all(not s.get('highlights') for s in data['sources'])


async def test_parent_version_scope_is_not_detached_from_matching_child(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<h1>Version 3.2 only</h1>
        <p>These features are experimental. Not available in 3.1.</p>
        <h2>Introduction</h2><p>Background information.</p>
        <h2>open_safe</h2><p>open_safe enables transactions.</p>
        <h2>Warning</h2><p>Never enable this for production workloads.</p>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'query': 'open_safe'}))['sources'][0]
        quote = source['highlights'][0]['text']
        assert 'Version 3.2 only' in quote
        assert 'Not available in 3.1.' in quote
        assert 'Never enable this for production workloads.' in quote


async def test_highlight_limits_are_explicit_and_quotes_are_atomic(tmp_path):
    from retrieval_helpers import running, client
    async def handler(request):
        html = ''.join(f'<h2>option_safe {i}</h2><p>option_safe version 2.{i} only. Do not use version 1.</p>'
                       for i in range(6))
        return web.Response(content_type='text/html', text=html)
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [base], 'query': 'option_safe'})
        data = body(result)
        source = data['sources'][0]
        assert len(source['highlights']) == 3
        assert source['selection_omitted_highlight_count'] == 3
        assert data['selection']['partial_coverage'] and data['budget']['partial_coverage']
        full = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only'}))['sources'][0]
        small = await session.call_tool('fetch', {'urls': [base], 'query': 'option_safe', 'response_budget_bytes': 4096})
        limited = body(small)
        assert len(small.model_dump_json(exclude_none=True).encode()) <= 4096
        for item in limited['sources']:
            for highlight in item.get('highlights', []):
                assert highlight['text'] == full['content'][highlight['start']:highlight['end']]
                assert 'Do not use version 1.' in highlight['text']
        assert limited['budget']['partial_coverage']


async def test_search_explicit_modes_and_twelve_pages_have_no_hidden_cap(tmp_path):
    visits = []
    async def handler(request):
        visits.append(request.path)
        # Realistic text-bearing HTML: upstream treats near-empty renders as
        # potential block pages; acquisition heuristics are outside this ticket.
        return web.Response(content_type='text/html', text=f'<h1>Guide {request.path}</h1><p>option_safe version 2 only.</p>'
                            '<p>This technical reference describes supported client behavior and compatibility constraints.</p>')
    async with site(handler) as (base, options):
        payload = {'data': [{'url': f'{base}/{i}', 'title': f'Guide {i}', 'rank': i + 1} for i in range(12)]}
        async with running(tmp_path, source=cli_response(json.dumps(payload)), acquisition_options=options) as (url, _), client(url, timeout=60) as session:
            discovery = body(await session.call_tool('search', {'query': 'option_safe', 'num_results': 12, 'content_mode': 'discovery'}))
            assert len(discovery.get('results', [])) == 12 and visits == [], discovery
            result = body(await session.call_tool('search', {'query': 'option_safe', 'num_results': 12, 'content_mode': 'full_text'}))
            assert len(result['results']) == 12 and len(visits) == 12
            assert all('option_safe' in s.get('content', '') for s in result['results']), [s.get('error') for s in result['results']]
            assert result['acquisition_limits']['request_deadline_seconds'] == 300
            assert result['acquisition_limits']['post_acquisition_document_bytes'] == 2 * 1024 * 1024
            assert result['acquisition_limits']['returned_page_limit'] == 20


async def test_exact_document_duplicates_keep_attribution_but_different_qualifications_survive(tmp_path):
    async def handler(request):
        warning = 'not safe in 2.3' if request.path == '/qualified' else 'safe in 2.3'
        return web.Response(content_type='text/html', text=f'<h1>connect_safe</h1><p>connect_safe is {warning}.</p>'
                            '<p>This technical reference describes supported client behavior and compatibility constraints.</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        sources = body(await session.call_tool('fetch', {'urls': [base + '/one', base + '/two', base + '/qualified'], 'query': 'connect_safe'}))['sources']
        assert 'error' not in sources[0], [s.get('error') for s in sources]
        assert sources[0]['highlights']
        assert sources[1]['highlights'] == []
        duplicate = sources[1]['duplicate_evidence'][0]
        assert duplicate['duplicate_of'] == sources[0]['highlights'][0]['evidence_id']
        assert duplicate['url'] == base + '/two' and duplicate['evidence_id']
        assert 'not safe in 2.3' in sources[2]['highlights'][0]['text']


async def test_encoded_credential_echo_never_becomes_highlights_or_cached_evidence(tmp_path, caplog):
    from retrieval_helpers import running, client
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>connect_safe</h1><p>connect_safe reflected subscriber%2Dsecret.</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'})
        assert body(result)['sources'][0]['error']['code'] == 'fetch_failed'
        assert 'subscriber' not in result.model_dump_json() + caplog.text
        cached = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only'}))
        assert cached['sources'][0]['error']['code'] == 'cache_miss'
