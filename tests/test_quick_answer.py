"""Pinned subscriber `quick` CLI contract and discovery-only search defaults."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from vresto.budget import apply_budget, wire_bytes
from vresto.contract import SearchInput, SearchRequest
from test_http import alive, body, client, running
from test_retrieval import retrieval
from test_summaries import document


QUERY = '--query ; $(oops)'
RESULTS = [{'url': 'https://example.org/docs', 'title': 'Docs', 'rank': 1,
            'snippet': 'Provider summary'}]
QUICK = {'meta': {'trace': 'private-upstream-trace'}, 'query': QUERY,
         'message': {'markdown': 'Generated answer [1].', 'html': '<p>Not returned</p>'},
         'references': {'markdown': 'Not returned', 'items': [
             {'index': 1, 'title': 'Docs', 'url': 'https://example.org/docs',
              'domain': 'example.org', 'contribution_pct': 100}]},
         'followup_questions': ['Not returned']}
ANSWER = {'text': 'Generated answer [1].', 'generated': True, 'provider': 'kagi',
          'references': [{'index': 1, 'title': 'Docs', 'url': 'https://example.org/docs'}]}


def cli(tmp_path, *, quick=None, search_action='', quick_action=''):
    payload = QUICK if quick is None else quick
    return f'''import json, os, sys, time
from pathlib import Path
if '--version' in sys.argv:
    assert 'KAGI_SESSION_TOKEN' not in os.environ
    print('kagi 0.22.0')
else:
    args = sys.argv[1:]
    assert args[:2] == ['--error-format', 'json']
    assert args[-2:] == ['--', {QUERY!r}]
    assert os.environ['KAGI_SESSION_TOKEN'] == 'subscriber-secret'
    assert 'subscriber-secret' not in str(args)
    assert 'client-secret' not in str(args) + str(os.environ)
    assert set(os.environ) <= {{'PATH', 'HOME', 'KAGI_CONFIG', 'KAGI_SESSION_TOKEN', 'RUST_LOG', 'LC_CTYPE'}}
    assert not Path(os.environ['KAGI_CONFIG']).exists()
    command = args[2]
    with open({str(tmp_path / 'calls.jsonl')!r}, 'a') as file:
        file.write(json.dumps(args) + '\\n')
    Path({str(tmp_path)!r}, command + '.entered').write_text(str(os.getpid()))
    if command == 'search':
        assert args[3:7] == ['--format', 'json', '--limit', '10']
        {search_action or 'pass'}
        print({json.dumps({'data': RESULTS})!r})
    elif command == 'quick':
        assert args[3:5] == ['--format', 'json']
        {quick_action or 'pass'}
        print({json.dumps(payload)!r})
    else:
        raise AssertionError('unexpected command')
'''


def calls(tmp_path):
    return [json.loads(line)[2] for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]


@pytest.mark.parametrize('model', [SearchInput, SearchRequest])
@pytest.mark.parametrize('enabled', [None, False, True])
async def test_default_search_returns_discovery_and_optional_quick_without_acquisition_or_memory(tmp_path, model, enabled):
    async with retrieval(tmp_path, source=cli(tmp_path)) as service:
        service.cached_acquisition.fetch_many = AsyncMock(side_effect=AssertionError('must not acquire'))
        service.memory = SimpleNamespace(query=AsyncMock(side_effect=AssertionError('must not query memory')))
        arguments = {'query': QUERY}
        if enabled is not None:
            arguments['quick_answer'] = enabled
        data = await service.search(model.model_validate(arguments))
        assert data['content_mode'] == 'discovery'
        assert data['results'] == [{'url': RESULTS[0]['url'], 'title': 'Docs',
                                    'provider_rank': 1, 'provider_snippet': 'Provider summary'}]
        assert 'acquisition_limits' not in data and 'selection' not in data
        service.cached_acquisition.fetch_many.assert_not_awaited()
        service.memory.query.assert_not_awaited()
        if enabled is False:
            assert calls(tmp_path) == ['search']
            assert 'quick_answer' not in data and 'quick_answer_error' not in data
        else:
            assert sorted(calls(tmp_path)) == ['quick', 'search']
            assert data['quick_answer'] == ANSWER
            assert 'quick_answer_error' not in data
            assert 'private-upstream-trace' not in json.dumps(data)


@pytest.mark.parametrize('model', [SearchInput, SearchRequest])
@pytest.mark.parametrize('value', ['true', 1, None])
def test_quick_answer_flag_is_a_strict_boolean(model, value):
    with pytest.raises(ValidationError):
        model(query='docs', quick_answer=value)


@pytest.mark.parametrize('mode', ['highlights', 'full_text'])
async def test_explicit_acquisition_modes_still_include_optional_quick_answer(tmp_path, mode):
    async with retrieval(tmp_path, source=cli(tmp_path)) as service:
        canonical = document(text='# Guide\n\nQuery guide documents supported constraints.\n')
        service.cached_acquisition.fetch_many = AsyncMock(return_value={'sources': [canonical]})
        data = await service.search(SearchInput(query=QUERY, content_mode=mode))
        service.cached_acquisition.fetch_many.assert_awaited_once()
        assert data['content_mode'] == mode
        assert data['quick_answer'] == ANSWER
        assert 'acquisition_limits' in data


async def test_quick_answer_uses_the_same_internal_lens(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path)) as service:
        data = await service.search(SearchRequest(query=QUERY, lens=3))
        assert data['quick_answer'] == ANSWER
        for line in (tmp_path / 'calls.jsonl').read_text().splitlines():
            args = json.loads(line)
            assert args[args.index('--lens') + 1] == '3'


@pytest.mark.parametrize('payload', [
    {}, {'message': {'markdown': ''}, 'references': {'items': []}},
    {'message': {'markdown': '  '}, 'references': {'items': []}},
    {'message': {'markdown': 42}, 'references': {'items': []}},
    {**QUICK, 'references': {'items': {}}},
    {**QUICK, 'references': {'items': [{'index': 1, 'title': 'Unsafe', 'url': 'file:///etc/passwd'}]}},
    {**QUICK, 'references': {'items': [{'index': True, 'title': 'Docs', 'url': 'https://example.org'}]}},
    {**QUICK, 'references': {'items': [{'index': 1, 'title': 'Unsafe', 'url': 'https://user:pass@example.org'}]}},
    {**QUICK, 'message': {'markdown': 'subscriber%2Dsecret'}},
    {**QUICK, 'meta': {'trace': 'client-secret'}},
])
async def test_invalid_or_credential_bearing_quick_answer_does_not_discard_links(tmp_path, payload):
    async with retrieval(tmp_path, source=cli(tmp_path, quick=payload)) as service:
        data = await service.search(SearchInput(query=QUERY))
        assert len(data['results']) == 1
        assert data['quick_answer_error'] == {'code': 'discovery_parse_error'}
        assert 'quick_answer' not in data
        assert 'subscriber' not in json.dumps(data) and 'client-secret' not in json.dumps(data)


@pytest.mark.parametrize('action,code', [
    ("print('invalid JSON'); sys.exit(0)", 'discovery_parse_error'),
    ("print('subscriber-secret', file=sys.stderr); sys.exit(1)", 'discovery_failed'),
    ("print(json.dumps({'code': 'authentication_error'}), file=sys.stderr); sys.exit(1)", 'upstream_authentication'),
    ("print(json.dumps({'http_status': 429}), file=sys.stderr); sys.exit(1)", 'rate_limited'),
    ("print('x' * 10000); sys.exit(0)", 'discovery_output_limit'),
])
async def test_quick_cli_failures_are_sanitized_and_preserve_search(tmp_path, action, code):
    async with retrieval(tmp_path, source=cli(tmp_path, quick_action=action), max_output_bytes=4096) as service:
        data = await service.search(SearchInput(query=QUERY))
        assert len(data['results']) == 1
        assert data['quick_answer_error'] == {'code': code}
        assert 'quick_answer' not in data
        assert not service.discovery.processes.active
        assert service.discovery.processes.admitted == 0


async def test_search_and_quick_run_concurrently_with_shared_cli_capacity(tmp_path):
    # Each command requires its sibling to have started. Serial execution fails.
    gate = f"\n        while not all(Path({str(tmp_path)!r}, name + '.entered').exists() for name in ['search', 'quick']): time.sleep(.01)"
    async with retrieval(tmp_path, source=cli(tmp_path, search_action=gate, quick_action=gate), timeout=2) as service:
        data = await service.search(SearchInput(query=QUERY))
        assert data['quick_answer'] == ANSWER and len(data['results']) == 1
        assert service.discovery.processes.admitted == 0
        assert not service.discovery.processes.active


async def test_quick_deadline_keeps_completed_links_and_reaps_process(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path, quick_action='time.sleep(5)'), timeout=.3) as service:
        data = await service.search(SearchInput(query=QUERY))
        assert len(data['results']) == 1
        assert data['quick_answer_error'] == {'code': 'discovery_timeout'}
        assert not alive(int((tmp_path / 'quick.entered').read_text()))
        assert not service.discovery.processes.active
        assert service.discovery.processes.admitted == 0


async def test_failed_search_cancels_in_flight_quick_answer(tmp_path):
    gate = f"\n        while not Path({str(tmp_path)!r}, 'quick.entered').exists(): time.sleep(.01)\n        print('failure', file=sys.stderr); sys.exit(1)"
    async with retrieval(tmp_path, source=cli(tmp_path, search_action=gate, quick_action='time.sleep(5)')) as service:
        data = await service.search(SearchInput(query=QUERY))
        assert data['error']['code'] == 'discovery_failed'
        assert not alive(int((tmp_path / 'quick.entered').read_text()))
        assert not service.discovery.processes.active
        assert service.discovery.processes.admitted == 0


async def test_caller_cancellation_reaps_both_commands(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path, search_action='time.sleep(5)', quick_action='time.sleep(5)')) as service:
        task = asyncio.create_task(service.search(SearchInput(query=QUERY)))
        try:
            async with asyncio.timeout(2):
                while not all((tmp_path / (name + '.entered')).exists() for name in ['search', 'quick']):
                    await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not service.discovery.processes.active
            assert service.discovery.processes.admitted == 0
            assert not service.discovery.processes.waiters
            assert all(not alive(int((tmp_path / (name + '.entered')).read_text())) for name in ['search', 'quick'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('large', ['text', 'references'])
def test_quick_answer_budget_sheds_generated_payload_before_links(large):
    answer = {**ANSWER, 'text': 'Generated answer. ' * 2000 if large == 'text' else ANSWER['text']}
    if large == 'references':
        answer['references'] = [{'index': i, 'title': 'Reference', 'url': 'https://example.org/' + 'x' * 1000}
                                for i in range(20)]
    links = [{'url': 'https://example.org/1', 'title': 'Docs', 'provider_snippet': 'Provider summary'}]
    data = {'content_mode': 'discovery', 'results': links, 'quick_answer': answer}
    bounded = apply_budget(data, 4096)
    assert wire_bytes(bounded) <= 4096
    assert bounded['results'] == links
    assert bounded['budget']['partial_coverage']
    assert bounded['quick_answer']['generated']
    if large == 'text':
        assert bounded['quick_answer']['response_truncated']
        assert data['quick_answer']['text'] == answer['text']
    else:
        assert bounded['quick_answer']['omitted_reference_count'] > 0
        assert len(data['quick_answer']['references']) == 20


async def test_mcp_schema_and_default_search_expose_quick_answer_option(tmp_path):
    async with running(tmp_path, source=cli(tmp_path)) as (url, _), client(url) as session:
        tool = (await session.list_tools()).tools[0]
        assert tool.inputSchema['properties']['content_mode']['default'] == 'discovery'
        assert tool.inputSchema['properties']['quick_answer']['default'] is True
        assert tool.inputSchema['properties']['quick_answer']['type'] == 'boolean'
        result = await session.call_tool('search', {'query': QUERY})
        assert not result.isError
        data = body(result)
        assert data['content_mode'] == 'discovery'
        assert data['quick_answer'] == ANSWER
        assert len(data['results']) == 1
