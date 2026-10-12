from test_http import running, client, body
from test_fetch import site
from test_pdf import pdf_bytes
from aiohttp import web


async def test_pdf_wire_expansion_preserves_canonical_quote_pages_context_and_budget(tmp_path):
    hits = []
    async def handler(request):
        hits.append(request.path)
        return web.Response(content_type='application/pdf', body=pdf_bytes([
            'Previous page qualification. ' * 150,
            'Version 2 connect_safe requires TLS. Never disable verification.',
            'Following page limitation. ' * 150]))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        full = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        selected = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
        quote = selected['highlights'][0]
        assert quote['pages'] == [2]
        expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
        assert expanded['content_version'] == full['content_version']
        assert expanded['cache_state'] == 'retained_version'
        assert expanded['highlights'] == [quote]
        assert expanded['highlights'][0]['pages'] == [2]
        assert [context['pages'] for context in expanded['context']] == [[1], [3]]
        for context in expanded['context']:
            assert context['text'] == full['content'][context['start']:context['end']]
        bounded = await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']], 'response_budget_bytes': 4096})
        assert len(bounded.model_dump_json(exclude_none=True).encode()) <= 4096
        data = body(bounded)
        retained = data['sources'][0]
        assert retained['highlights'] == [quote]
        assert retained['context'] == []
        assert retained['omitted_context_section_count'] == 2
        assert data['budget']['partial_coverage'] is True
        assert hits == ['/']
