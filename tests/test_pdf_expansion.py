from test_http import running, client, body
from test_fetch import site
from test_pdf import pdf_bytes
from aiohttp import web
import pytest


@pytest.mark.parametrize('repetitions', [1, 100])
async def test_initial_pdf_highlight_preserves_both_neighbor_qualifications_atomically(tmp_path, repetitions):
    async def handler(request):
        return web.Response(content_type='application/pdf', body=pdf_bytes([
            'Experimental only. Never use this interface in production. ' * repetitions,
            'connect_safe enables transactions.',
            'Warning: do not disable validation. Unsupported on legacy systems. ' * repetitions]))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        full = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        selected = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
        assert 'error' not in selected, selected
        quote = selected['highlights'][0]
        assert quote['pages'] == [1, 2, 3]
        assert 'Experimental only.' in quote['text'] and 'Never use' in quote['text']
        assert 'do not disable validation.' in quote['text'] and 'Unsupported' in quote['text']
        assert quote['text'] == full['content'][quote['start']:quote['end']]
        assert quote['start'] == full['pdf']['references'][0]['start']
        assert quote['end'] == full['pdf']['references'][2]['end']
        expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
        assert expanded['highlights'] == [quote]
        assert expanded['context'] == []
        budget = 8192 if repetitions == 1 else 4096
        bounded = await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe', 'response_budget_bytes': budget})
        data = body(bounded)
        assert len(bounded.model_dump_json(exclude_none=True).encode()) <= budget
        returned = [h for s in data['sources'] for h in s.get('highlights', [])]
        if repetitions == 1:
            assert returned == [quote]
        else:
            assert returned == [] and data['budget']['partial_coverage']


async def test_pdf_wire_expansion_preserves_canonical_quote_pages_context_and_budget(tmp_path):
    hits = []
    async def handler(request):
        hits.append(request.path)
        return web.Response(content_type='application/pdf', body=pdf_bytes([
            'Previous distant context. ' * 150,
            'Experimental only. Never deploy in production.',
            'Version 2 connect_safe requires TLS. Never disable verification.',
            'Warning: not supported in legacy mode.',
            'Following distant context. ' * 150]))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        full = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        selected = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
        quote = selected['highlights'][0]
        assert quote['pages'] == [2, 3, 4]
        expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
        assert expanded['content_version'] == full['content_version']
        assert expanded['cache_state'] == 'retained_version'
        assert expanded['highlights'] == [quote]
        assert expanded['highlights'][0]['pages'] == [2, 3, 4]
        assert [context['pages'] for context in expanded['context']] == [[1], [5]]
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
