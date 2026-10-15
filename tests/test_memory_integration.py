from copy import deepcopy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from kagi_crawler.budget import apply_budget, wire_bytes
from kagi_crawler.contract import ExpandInput, FetchInput, SearchInput
from kagi_crawler.evidence import evidence_id, resolve_evidence
from kagi_crawler.memory import Memory
from kagi_crawler.server import Settings
from test_http import cli_response, running
from test_memory import FakeEmbedder, source, store
from test_retrieval import retrieval


async def test_successful_acquisition_enqueues_without_waiting_and_screens_secrets(tmp_path, monkeypatch):
    async with retrieval(tmp_path, source=cli_response('{"data": []}')) as service:
        memory = Memory(store(tmp_path), FakeEmbedder())
        service.memory = memory
        service.cached_acquisition.on_success = memory.submit
        canonical = source()
        monkeypatch.setattr(service.acquisition, 'fetch_many', AsyncMock(return_value={
            'sources': [canonical], 'content_mode': 'full_text'}))
        fetched = await service.fetch(FetchInput(urls=[canonical['requested_url']]))
        assert fetched['sources'][0]['content'] == canonical['content']
        await memory.queue.join()
        assert memory.usage()['passages'] == 1
        contaminated = source('subscriber%2Dsecret', 'https://example.org/secret')
        monkeypatch.setattr(service.acquisition, 'fetch_many', AsyncMock(return_value={
            'sources': [contaminated], 'content_mode': 'full_text'}))
        fetched = await service.fetch(FetchInput(urls=[contaminated['requested_url']]))
        assert fetched['sources'][0]['error']['code'] == 'fetch_failed'
        await memory.queue.join()
        assert memory.usage()['passages'] == 1
        await memory.close()


@pytest.mark.parametrize('mode', ['discovery', 'highlights', 'full_text', 'summary'])
async def test_search_merges_retained_passages_and_fresh_only_excludes_them(tmp_path, mode):
    async with retrieval(tmp_path, source=cli_response('{"data": []}')) as service:
        memory = Memory(store(tmp_path), FakeEmbedder())
        service.memory = memory
        s = source()
        memory.submit(s)
        await memory.queue.join()
        data = await service.search(SearchInput(query='kitten', content_mode=mode))
        retained = data['results'][0]
        assert retained['origin'] == 'memory' and retained['fetched_at'] == s['fetched_at']
        assert retained['highlights'][0]['text'] == s['content']
        assert retained['content_mode'] == 'highlights' and 'summary' not in retained
        fresh = await service.search(SearchInput(query='kitten', content_mode=mode, freshness='fresh_only'))
        assert fresh['results'] == []
        await memory.close()


async def test_live_urls_excluded_and_memory_ids_expand_after_cache_eviction(tmp_path):
    live = source()
    payload = json.dumps({'data': [{'url': live['requested_url'], 'title': 'Cats', 'rank': 1}]})
    async with retrieval(tmp_path, source=cli_response(payload)) as service:
        memory = Memory(store(tmp_path), FakeEmbedder())
        service.memory = memory
        memory.submit(live)
        old = source('A kitten sleeps.', 'https://example.org/old', '2000-01-01T00:00:00+00:00')
        memory.submit(old)
        await memory.queue.join()
        service.cache.store(old)
        service.cache._path(old['requested_url'], old['content_version']).unlink(missing_ok=True)
        data = await service.search(SearchInput(query='kitten', content_mode='discovery'))
        assert len(data['results']) == 2
        assert 'origin' not in data['results'][0]
        assert data['results'][1]['requested_url'] == old['requested_url']
        identifier = data['results'][1]['highlights'][0]['evidence_id']
        expanded = await service.expand(ExpandInput(evidence_ids=[identifier]))
        assert expanded['sources'][0]['origin'] == 'memory'
        assert expanded['sources'][0]['fetched_at'] == old['fetched_at']
        assert expanded['sources'][0]['highlights'][0]['text'] == old['content']
        assert resolve_evidence(service.cache, evidence_id(old, 0, len(old['content'])), memory.store)[0]['content'] == old['content']
        memory.store.db.execute('DELETE FROM documents')
        memory.store.db.commit()
        unavailable = await service.expand(ExpandInput(evidence_ids=[identifier]))
        assert unavailable['sources'][0]['error']['code'] == 'evidence_unavailable'
        await memory.close()


def test_memory_retains_pdf_and_partial_coverage_disclosures(tmp_path):
    db = store(tmp_path)
    canonical = source('# PDF page 1\n\nA feline.\n\n')
    canonical['document_type'] = 'pdf'
    canonical['pdf'] = {'total_pages': 2, 'processed_pages': 1, 'omitted_pages': 1,
        'unsupported_pages': [], 'partial': True, 'references': [{'page': 1, 'start': 0,
        'end': len(canonical['content']), 'status': 'text'}], 'parser': 'test', 'layout': 'test'}
    canonical['coverage'] = {'partial': True, 'site_adapter': {'name': 'test', 'outcome': 'applied', 'reason': 'public_preview'}}
    canonical['link_targets'] = ['https://example.org/private-metadata']
    db.put(canonical, 0, len(canonical['content']), 'PDF page 1', [1., 0.])
    result = db.query('feline', [1., 0.])[0]
    assert result['pdf']['partial'] and result['coverage']['partial']
    assert result['highlights'][0]['pages'] == [1]
    assert 'content' not in result and 'link_targets' not in result
    db.close()


def test_memory_never_displaces_live_content_in_shared_budget():
    live = {'url': 'https://example.org/live', 'content': 'live text ' * 150}
    retained = {'origin': 'memory', 'highlights': [{'text': 'old text ' * 1000}],
                'fetched_at': '2000-01-01T00:00:00+00:00'}
    data = {'results': [deepcopy(live), retained], 'content_mode': 'full_text'}
    bounded = apply_budget(data, 4096)
    assert wire_bytes(bounded) <= 4096
    assert bounded['results'] == [live]
    assert bounded['budget']['omitted_source_indices'] == [1]
    assert data['results'][0] == live


def test_explicit_memory_expansion_sheds_context_before_original_quote():
    quote = {'text': 'An exact retained quotation.', 'evidence_id': 'reference'}
    data = {'sources': [{'origin': 'memory', 'content_mode': 'evidence_expansion',
                        'highlights': [quote], 'context': [{'text': 'Neighbor context. ' * 1000}]}],
            'content_mode': 'evidence_expansion'}
    bounded = apply_budget(data, 4096)
    assert bounded['sources'][0]['highlights'] == [quote]
    assert bounded['sources'][0]['context'] == []
    assert bounded['sources'][0]['omitted_context_section_count'] == 1
    assert wire_bytes(bounded) <= 4096


@pytest.mark.parametrize('enabled,reason', [(False, 'operator_disabled'), (True, 'model_absent')])
async def test_disabled_memory_health_does_not_start_worker(tmp_path, enabled, reason):
    async with running(tmp_path, memory_enabled=enabled, memory_model_directory=tmp_path / 'missing') as (url, _):
        async with httpx.AsyncClient() as http:
            data = (await http.get(url.removesuffix('/mcp') + '/health',
                    headers={'Authorization': 'Bearer client-secret'})).json()
            assert data['memory'] == {'status': 'disabled', 'reason': reason, 'passages': 0,
                                      'bytes': 0, 'ingest_backlog': 0, 'ingest_drops': 0}
            assert data['browser'] == 'stopped'


async def test_enabled_memory_health_and_lifespan(tmp_path, monkeypatch):
    import kagi_crawler.server as server
    class FakeWorker(FakeEmbedder):
        available = True
        def __init__(self, *args, **kwargs):
            pass
    monkeypatch.setattr(server, 'WorkerEmbedder', FakeWorker)
    async with running(tmp_path) as (url, _):
        async with httpx.AsyncClient() as http:
            data = (await http.get(url.removesuffix('/mcp') + '/health',
                    headers={'Authorization': 'Bearer client-secret'})).json()
            assert data['memory']['status'] == 'enabled'
            assert data['memory']['passages'] == data['memory']['ingest_backlog'] == 0
            assert data['browser'] == 'stopped'


@pytest.mark.parametrize('kwargs', [
    {'memory_cap_bytes': 100}, {'memory_cap_bytes': 10_737_418_241},
    {'memory_retention_seconds': -1}, {'memory_retention_seconds': float('inf')},
    {'memory_threads': 0}, {'memory_threads': 17}, {'memory_dimensions': 42},
    {'memory_queue_capacity': 0}, {'memory_queue_capacity': 65},
    {'memory_worker_bytes': 1}, {'memory_worker_bytes': 17_179_869_185},
])
def test_memory_settings_bounds(kwargs):
    with pytest.raises(ValueError):
        Settings(access_token='test', session_token='test', **kwargs)
