"""Executable CLI contract fixtures; no live subscriber credentials or browser."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time

import pytest

from kagi_crawler.acquisition_policy import AcquisitionOptions
from kagi_crawler.budget import apply_budget, wire_bytes
from kagi_crawler.cache import DocumentCache
from kagi_crawler.contract import FetchInput, SearchInput
from kagi_crawler.summaries import MAX_INPUT_BYTES, summary_input
from test_http import body, client, running
from test_retrieval import retrieval


def document(index=1, text=None):
    text = text or f'# Retries {index}\n\nRetries require version {index}. Never retry older versions.\n'
    return {'requested_url': f'https://example.org/{index}',
            'resolved_url': f'https://example.org/{index}', 'title': 'Retries',
            'document_type': 'html', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'content_version': hashlib.sha256(text.encode()).hexdigest(),
            'content_mode': 'full_text', 'content': text}


def cli(tmp_path, action=None):
    return f'''import json, os, sys, time
from pathlib import Path
if '--version' in sys.argv:
    assert 'KAGI_SESSION_TOKEN' not in os.environ
    print('kagi 0.22.0')
elif 'search' in sys.argv:
    print(json.dumps({{"data": [{{"url": "https://example.org/1", "title": "Retries"}}]}}))
else:
    assert os.environ['KAGI_SESSION_TOKEN'] == 'subscriber-secret'
    assert 'subscriber-secret' not in str(sys.argv)
    assert 'client-secret' not in str(sys.argv) + str(os.environ)
    assert set(os.environ) <= {{'PATH', 'HOME', 'KAGI_CONFIG', 'KAGI_SESSION_TOKEN', 'RUST_LOG', 'LC_CTYPE'}}
    assert not Path(os.environ['KAGI_CONFIG']).exists()
    args = sys.argv[1:]
    assert args[:4] == ['--error-format', 'json', 'summarize', '--subscriber']
    assert args[4].startswith('--text=')
    assert args[-2:] == ['--summary-type', 'summary']
    with open({str(tmp_path / 'calls.jsonl')!r}, 'a') as f:
        f.write(json.dumps({{"args": args, "start": time.time()}}) + '\\n')
    {action or "print(json.dumps({'meta': {}, 'data': {'markdown': 'Generated retry summary.'}}))"}
'''


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]


async def test_summary_modes_over_mcp_persist_cache_and_keep_exact_quotes(tmp_path):
    canonical = document()
    cache = DocumentCache(tmp_path / 'cache')
    cache.store(canonical)
    fixture = cli(tmp_path)
    async with running(tmp_path, source=fixture) as (url, _), client(url) as session:
        for query in (None, 'retries'):
            arguments = {'urls': [canonical['requested_url']], 'content_mode': 'summary'}
            if query:
                arguments['query'] = query
            response = await session.call_tool('fetch', arguments)
            assert not response.isError
            data = body(response)
            source = data['sources'][0]
            assert data['content_mode'] == source['content_mode'] == 'summary'
            assert 'content' not in source
            assert source['summary'] == {
                'text': 'Generated retry summary.', 'generated': True, 'provider': 'kagi',
                'length': 'digest', 'type': 'summary',
                'content_version': canonical['content_version'], 'summary_input_truncated': False}
            if query:
                assert source['highlights'][0]['text'] == canonical['content']
                assert source['highlights'][0]['evidence_id']
            else:
                assert 'highlights' not in source
        assert len(calls(tmp_path)) == 1
        assert calls(tmp_path)[0]['args'][4].removeprefix('--text=') == canonical['content']
        result = body(await session.call_tool('search', {'query': 'retries', 'content_mode': 'summary'}))
        assert result['results'][0]['summary']['length'] == 'overview'
        assert result['results'][0]['highlights'][0]['text'] == canonical['content']
        assert len(calls(tmp_path)) == 2
    # Separate process lifecycle, same persisted cache; lengths have distinct keys.
    async with running(tmp_path, source=fixture) as (url, _), client(url) as session:
        await session.call_tool('fetch', {'urls': [canonical['requested_url']], 'content_mode': 'summary'})
    assert len(calls(tmp_path)) == 2
    assert cache.lookup(canonical['requested_url']) == canonical


@pytest.mark.parametrize('action,reason', [
    ("print('invalid JSON')", 'discovery_parse_error'),
    ("print(json.dumps({'data': {'markdown': ''}}))", 'discovery_parse_error'),
    ("print(json.dumps({'data': {'markdown': 'subscriber-secret'}}))", 'discovery_parse_error'),
    ("print(json.dumps({'code': 'authentication_error'}), file=sys.stderr); sys.exit(1)", 'upstream_authentication'),
    ("print(json.dumps({'http_status': 429}), file=sys.stderr); sys.exit(1)", 'rate_limited'),
    ("print('X' * 1100000)", 'discovery_output_limit'),
])
async def test_summary_failures_are_per_source_and_keep_highlights(tmp_path, action, reason):
    async with retrieval(tmp_path, source=cli(tmp_path, action)) as service:
        source = document()
        service.cache.store(source)
        result = await service.fetch(FetchInput(urls=[source['requested_url']], query='retries', content_mode='summary'))
        returned = result['sources'][0]
        assert 'error' not in returned and 'summary' not in returned
        assert returned['summary_error'] == {'code': 'summary_unavailable', 'reason': reason}
        assert returned['highlights'][0]['text'] == source['content']
        assert 'subscriber-secret' not in json.dumps(result)
        assert service.discovery.processes.active == set()


async def test_summary_deadline_includes_acquisition_and_kills_cli(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    async with retrieval(tmp_path, source=cli(tmp_path, 'time.sleep(30)'),
                         acquisition_options=AcquisitionOptions(deadline=0.15)) as service:
        async def acquire(*args, **kwargs):
            await asyncio.sleep(0.08)
            return {'sources': [document()], 'content_mode': 'full_text'}
        monkeypatch.setattr(service.cached_acquisition, 'fetch_many', AsyncMock(side_effect=acquire))
        start = time.monotonic()
        result = await service.fetch(FetchInput(urls=['https://example.org/1'], content_mode='summary'))
        assert time.monotonic() - start < 2
        assert result['sources'][0]['summary_error']['reason'] == 'discovery_timeout'
        assert not service.discovery.processes.active
        assert not service.discovery.processes.admitted


async def test_summaries_run_in_parallel_with_fixed_concurrency(tmp_path):
    action = "time.sleep(0.15); print(json.dumps({'data': {'markdown': 'Generated.'}}))"
    async with retrieval(tmp_path, source=cli(tmp_path, action)) as service:
        docs = [document(i) for i in range(1, 6)]
        for source in docs:
            service.cache.store(source)
        result = await service.fetch(FetchInput(urls=[s['requested_url'] for s in docs], content_mode='summary'))
        assert all('summary' in s for s in result['sources'])
        starts = sorted(c['start'] for c in calls(tmp_path))
        assert len(starts) == 5
        assert starts[1] - starts[0] < 0.14
        assert starts[2] - starts[0] >= 0.15
        assert starts[4] - starts[2] >= 0.15


def test_summary_input_utf8_section_and_paragraph_bounds():
    assert summary_input('small') == ('small', False)
    first = '# First\n\n' + 'é' * 20000
    text = first + '\n# Second\n\n' + '漢' * MAX_INPUT_BYTES
    assert summary_input(text) == (first, True)
    assert summary_input('first\n\n' + 'é' * MAX_INPUT_BYTES) == ('first', True)
    prefix, truncated = summary_input('漢' * MAX_INPUT_BYTES)
    assert truncated and len(prefix.encode()) <= MAX_INPUT_BYTES


async def test_truncated_summary_input_is_disclosed_and_versions_miss_cache(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path)) as service:
        source = document(text='# First\n\nHello.\n# Second\n\n' + 'x' * MAX_INPUT_BYTES)
        service.cache.store(source)
        request = FetchInput(urls=[source['requested_url']], content_mode='summary')
        result = await service.fetch(request)
        assert result['sources'][0]['summary']['summary_input_truncated'] is True
        assert calls(tmp_path)[0]['args'][4].removeprefix('--text=') == '# First\n\nHello.'
        service.cache.store(document())
        await service.fetch(request)
        assert len(calls(tmp_path)) == 2


def test_summary_cache_respects_shared_bounds_and_retention(tmp_path):
    cache = DocumentCache(tmp_path, cap_bytes=1024)
    fetched = datetime.now(timezone.utc).isoformat()
    cache.store_summary('v1', 'overview', {'text': 'x' * 600}, fetched)
    cache.store_summary('v2', 'overview', {'text': 'y' * 600}, fetched)
    assert cache.usage()['bytes'] <= 1024
    assert cache.summary('v1', 'overview') is None
    assert cache.summary('v2', 'overview') is not None
    cache.store_summary('v3', 'overview', {'text': 'z' * 2000}, fetched)
    assert cache.summary('v3', 'overview') is None
    cache.retention_seconds = 0
    assert cache.summary('v2', 'overview') is None


def test_budget_preserves_summary_before_atomic_highlights_and_discloses_shortening():
    summary = {'text': 'Generated. ' * 100, 'generated': True}
    data = {'content_mode': 'summary', 'sources': [{
        'summary': summary, 'highlights': [{'text': 'Quote. ' * 1000, 'evidence_id': 'id'}]}]}
    bounded = apply_budget(data, 4096)
    assert bounded['sources'][0]['summary'] == summary
    assert bounded['sources'][0]['highlights'] == []
    assert wire_bytes(bounded) <= 4096
    data['sources'][0]['summary']['text'] *= 20
    bounded = apply_budget(data, 4096)
    assert bounded['sources'][0]['summary']['response_truncated'] is True
    assert wire_bytes(bounded) <= 4096
    assert 'response_truncated' not in summary


async def test_search_summary_uses_public_small_surface(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path)) as service:
        service.cache.store(document())
        result = await service.search(SearchInput(query='retries', content_mode='summary'))
        assert result['results'][0]['summary']['length'] == 'overview'
        assert result['results'][0]['highlights']


async def test_failed_summary_does_not_discard_successful_sibling(tmp_path):
    action = ("if 'version 1' in args[4]: print('invalid JSON'); sys.exit(0)\n"
              "    print(json.dumps({'data': {'markdown': 'Sibling summary.'}}))")
    async with retrieval(tmp_path, source=cli(tmp_path, action)) as service:
        docs = [document(1), document(2)]
        for source in docs:
            service.cache.store(source)
        result = await service.fetch(FetchInput(urls=[s['requested_url'] for s in docs],
                                               query='retries', content_mode='summary'))
        assert result['sources'][0]['summary_error']['code'] == 'summary_unavailable'
        assert result['sources'][1]['summary']['text'] == 'Sibling summary.'
        assert all(s['highlights'] for s in result['sources'])


async def test_cancelling_summary_drains_cli_and_does_not_cache_failure(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path, 'time.sleep(30)')) as service:
        source = document()
        service.cache.store(source)
        task = asyncio.create_task(service.fetch(FetchInput(
            urls=[source['requested_url']], content_mode='summary')))
        async with asyncio.timeout(3):
            while not (tmp_path / 'calls.jsonl').exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service.discovery.processes.active
        assert not service.discovery.processes.admitted
        assert service.cache.summary(source['content_version'], 'digest') is None


async def test_dash_leading_canonical_markdown_is_a_literal_cli_value(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path)) as service:
        source = document(text='--subscriber\n\n- Retries require version 1.\n')
        service.cache.store(source)
        result = await service.fetch(FetchInput(urls=[source['requested_url']], content_mode='summary'))
        assert result['sources'][0]['summary']['generated'] is True
        assert calls(tmp_path)[0]['args'][4] == '--text=' + source['content']
