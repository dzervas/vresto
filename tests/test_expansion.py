import base64
import json
import pytest
from aiohttp import web
from test_fetch import site
from test_http import running, client, body


async def test_expansion_returns_exact_quote_and_surrounding_sections_without_visiting_page(tmp_path):
    visits = []
    async def handler(request):
        visits.append(request.raw_path)
        return web.Response(content_type='text/html', text='''<h1>Manual</h1>
        <h2>Before</h2><p>Background context for configuring the client safely.</p>
        <h2>connect_safe</h2><p>connect_safe requires version 2.4 only.</p>
        <h3>Warning</h3><p>Do not use retries in version 2.3.</p>
        <h2>After</h2><p>Additional diagnostic context for operating the client.</p>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        requested = base + '/manual?version=2.4&lang=en'
        original = body(await session.call_tool('fetch', {'urls': [requested], 'query': 'connect_safe'}))['sources'][0]
        quote = original['highlights'][0]
        result = await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]})
        assert not result.isError, body(result)
        data = body(result)
        expanded = data['sources'][0]
        assert data['content_mode'] == 'evidence_expansion'
        assert expanded['content_version'] == original['content_version']
        assert expanded['requested_url'] == requested
        assert expanded['highlights'] == [quote]
        assert 'Background context' in str(expanded['context'])
        assert 'Additional diagnostic context' in str(expanded['context'])
        assert {c['section'] for c in expanded['context']} >= {'Before', 'After'}
        assert visits == ['/manual?version=2.4&lang=en']
        # A well-shaped ID for a missing version is an unavailable source, not
        # a whole-request validation failure; preserve the successful reference.
        missing = 'p1.' + base64.urlsafe_b64encode(json.dumps(
            [requested, '0' * 64, 0, 1, '0' * 64], separators=(',', ':')).encode()).decode().rstrip('=')
        mixed = await session.call_tool('expand', {'evidence_ids': [quote['evidence_id'], missing]})
        assert not mixed.isError
        outcomes = body(mixed)
        assert outcomes['sources'][0]['highlights'] == [quote]
        assert outcomes['sources'][1]['error']['code'] == 'evidence_unavailable'
        assert outcomes['budget']['failed_source_count'] == 1
        assert outcomes['budget']['partial_coverage']
        assert visits == ['/manual?version=2.4&lang=en']


async def test_old_evidence_survives_refresh_restart_and_neighbor_version_eviction(tmp_path):
    visits = []
    version = '2.4'
    async def handler(request):
        visits.append(request.raw_path)
        return web.Response(content_type='text/html', text=f'''<h1>connect_safe</h1>
        <p>connect_safe requires version {version} only. Never enable unsupported retries.</p>
        <p>This reference documents compatibility constraints and operational safety.
        Consult the reference before deploying the client. Unsupported configuration can lose data.
        Operators should check all release requirements and read the warnings carefully.</p>''')
    async with site(handler) as (base, options):
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            original = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
            quote = original['highlights'][0]
            version = '9.9'
            current = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert current['content_version'] != original['content_version']
            result = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert result['highlights'] == [quote]
            assert result['fetched_at'] == original['fetched_at']
            assert '9.9' not in str(result['highlights'])
        # A new server instance uses only the same mounted canonical cache.
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            restarted = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert restarted['highlights'] == [quote]
            assert visits == ['/', '/']
        # Lowering the public operator cap at restart evicts the oldest version.
        async with running(tmp_path, acquisition_options=options, cache_cap_bytes=1024) as (url, _), client(url) as session:
            evicted = await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]})
            assert not evicted.isError
            assert body(evicted)['sources'][0]['error']['code'] == 'evidence_unavailable'
            retained = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'ordinary'}))['sources'][0]
            assert retained['content_version'] == current['content_version']
            assert '9.9' in retained['content']
            assert visits == ['/', '/']


@pytest.mark.parametrize('extra', [
    {'urls': ['https://example.org/']}, {'urls': None}, {'query': 'connect_safe'},
    {'query': None}, {'content_mode': 'full_text'}, {'content_mode': None},
    {'freshness': 'ordinary'}, {'freshness': 'fresh_only'}, {'max_age_seconds': 3600},
])
async def test_expansion_rejects_url_relevance_and_freshness_controls_before_visiting(tmp_path, extra):
    identifier = 'p1.' + base64.urlsafe_b64encode(json.dumps(
        ['https://example.org/', '0' * 64, 0, 1, '0' * 64], separators=(',', ':')).encode()).decode().rstrip('=')
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('expand', dict(extra, evidence_ids=[identifier]))
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'


@pytest.mark.parametrize('ids', [[], ['bad'], ['p1.%%%'], ['p1.W10'], ['p2.abc'], [None], 'p1.invalid', ['p1.' + 'x' * 32768]])
async def test_malformed_expansion_ids_are_validation_errors(tmp_path, ids):
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('expand', {'evidence_ids': ids})
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'


async def test_tampered_retained_quote_is_invalid_not_current_text(tmp_path):
    visits = []
    async def handler(request):
        visits.append(request.path)
        return web.Response(content_type='text/html', text='''<h1>connect_safe</h1>
        <p>connect_safe requires version 2.4 only. Never enable unsupported retries.</p>
        <p>This reference documents compatibility constraints and operational safety.</p>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
        identifier = source['highlights'][0]['evidence_id']
        payload = json.loads(base64.urlsafe_b64decode(identifier[3:] + '=' * (-len(identifier[3:]) % 4)))
        payload[2] += 1
        forged = 'p1.' + base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
        result = await session.call_tool('expand', {'evidence_ids': [identifier, forged]})
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'
        assert visits == ['/']


async def test_expansion_shared_budget_omits_whole_context_not_quotes(tmp_path):
    from retrieval_helpers import running, client
    visits = []
    async def handler(request):
        visits.append(request.path)
        return web.Response(content_type='text/html', text=f'''<h2>Before</h2><p>{'Detailed background material. ' * 200}</p>
        <h2>connect_safe</h2><p>connect_safe requires 2.4 only. Never use retries in 2.3.</p>
        <h2>After</h2><p>{'Additional diagnostic context. ' * 200}</p>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        quotes = []
        for path in ['/one', '/two']:
            source = body(await session.call_tool('fetch', {'urls': [base + path], 'query': 'connect_safe'}))['sources'][0]
            quotes.append(source['highlights'][0])
        full = body(await session.call_tool('expand', {'evidence_ids': [q['evidence_id'] for q in quotes]}))
        assert all(len(s['context']) == 2 for s in full['sources'])
        result = await session.call_tool('expand', {'evidence_ids': [q['evidence_id'] for q in quotes], 'response_budget_bytes': 4096})
        data = body(result)
        assert not result.isError
        assert len(result.model_dump_json(exclude_none=True).encode()) <= 4096
        assert data['budget']['partial_coverage']
        assert data['expansion']['budget_policy'] == 'omit_context_sections_then_whole_sources_preserve_quote_and_reference'
        assert data['sources']
        for source in data['sources']:
            assert source['highlights'][0] in quotes
            assert source['omitted_context_section_count'] == 2
            assert source['context'] == []
        assert visits == ['/one', '/two']


async def test_oversized_expansion_quote_omits_source_instead_of_shortening_it(tmp_path):
    from retrieval_helpers import running, client
    async def handler(request):
        return web.Response(content_type='text/html', text='<h2>connect_safe</h2><p>' +
            'connect_safe requires version 2.4 only. Never use retries in 2.3. ' * 80 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        original = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
        quote = original['highlights'][0]
        limited = await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']], 'response_budget_bytes': 4096})
        data = body(limited)
        assert len(limited.model_dump_json(exclude_none=True).encode()) <= 4096
        assert data['sources'] == []
        assert data['budget']['omitted_source_indices'] == [0]
        assert data['budget']['partial_coverage']
        intact = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
        assert intact['highlights'] == [quote]
