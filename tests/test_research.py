"""Pinned Assistant CLI boundary exercised by executable fixtures, not live auth."""
import asyncio
import json

import pytest

from vresto.budget import wire_bytes
from vresto.contract import ResearchInput
from vresto.research import Research
from test_http import body, client, running
from test_retrieval import retrieval
from test_summaries import calls, document


def cli(tmp_path, *, action=None, delete_error=False, reference_url='https://example.org/1',
        text='Generated retry answer [^1].'):
    payload = {'meta': {}, 'thread': {'id': 'thread-123'}, 'message': {
        'markdown': text, 'references_markdown': f'[^1]: [Retries]({reference_url})',
        'profile': {'model_name': 'account-model'}}}
    return f'''import json, os, sys, time
args = sys.argv[1:]
assert 'subscriber-secret' not in str(args)
assert 'client-secret' not in str(args) + str(os.environ)
if '--version' in args:
    assert 'KAGI_SESSION_TOKEN' not in os.environ
    print('kagi 0.22.0')
else:
    assert os.environ['KAGI_SESSION_TOKEN'] == 'subscriber-secret'
    assert set(os.environ) <= {{'PATH', 'HOME', 'KAGI_CONFIG', 'KAGI_SESSION_TOKEN', 'RUST_LOG', 'LC_CTYPE'}}
    with open({str(tmp_path / 'calls.jsonl')!r}, 'a') as f:
        f.write(json.dumps({{'args': args, 'start': time.time()}}) + '\\n')
    if 'thread' in args:
        assert args == ['--error-format', 'json', 'assistant', 'thread', 'delete', 'thread-123']
        if {delete_error!r}:
            print('{{"code":"network_error"}}', file=sys.stderr)
            sys.exit(1)
        print(json.dumps({{'deleted_thread_ids': ['thread-123']}}))
    else:
        assert args[:6] == ['--error-format', 'json', 'assistant', '--format', 'json', '--web-access']
        {action or f'print(json.dumps({payload!r}))'}
'''


@pytest.mark.parametrize('tier', ['small', 'big'])
async def test_research_success_literal_query_cleanup_and_progress(tmp_path, tier):
    progress = []

    async def observe(value, total, message):
        progress.append(message)

    async with running(tmp_path, source=cli(tmp_path), research_model='operator-model') as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': '--retries ; $(no)', 'tier': tier},
                                         progress_callback=observe)
        data = body(result)
        assert not result.isError
        assert data['answer']['generated'] is True
        assert data['tier'] == tier and data['model'] == 'account-model'
        assert data['thread_deleted'] is True
        assert data['references'] == [{'id': '1', 'title': 'Retries', 'url': 'https://example.org/1'}]
        assert 'verified_citations' not in data
    args = calls(tmp_path)[0]['args']
    assert args[-2:] == ['--', '--retries ; $(no)']
    assert ('--once' in args) == (tier == 'big')
    if tier == 'big':
        assert args[args.index('--model') + 1] == 'operator-model'
    else:
        assert '--model' not in args
    assert len(calls(tmp_path)) == 2
    assert progress == ['Research started']


async def test_big_requires_operator_model_without_spawning(tmp_path):
    async with running(tmp_path, source=cli(tmp_path)) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries', 'tier': 'big'})
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'
        assert body(result)['error']['reason'] == 'research_model_not_configured'
    assert not (tmp_path / 'calls.jsonl').exists()


@pytest.mark.parametrize('action,code', [
    ("print(json.dumps({'code': 'authentication_error'}), file=sys.stderr); sys.exit(1)", 'upstream_authentication'),
    ("print(json.dumps({'code': 'missing_credentials'}), file=sys.stderr); sys.exit(1)", 'upstream_authentication'),
    ("print(json.dumps({'http_status': 429}), file=sys.stderr); sys.exit(1)", 'rate_limited'),
    ("print(json.dumps({'code': 'parse_error'}), file=sys.stderr); sys.exit(1)", 'research_parse_error'),
    ("print('not JSON subscriber-secret')", 'research_parse_error'),
    ("print('X' * 100000)", 'research_output_limit'),
    ("time.sleep(30)", 'research_timeout'),
])
async def test_research_errors_are_fixed_and_cleanup_unknown_is_disclosed(tmp_path, action, code, caplog):
    async with running(tmp_path, source=cli(tmp_path, action=action), research_timeout=1) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries'})
        data = body(result)
        assert result.isError and data['error']['code'] == code
        assert data['thread_deleted'] is False
        assert 'subscriber-secret' not in json.dumps(data) + caplog.text
        assert 'client-secret' not in json.dumps(data) + caplog.text


async def test_thread_deletion_failure_does_not_fail_answer(tmp_path):
    async with running(tmp_path, source=cli(tmp_path, delete_error=True)) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries'})
        assert not result.isError
        assert body(result)['thread_deleted'] is False
        assert body(result)['answer']['text']
    assert len(calls(tmp_path)) == 2


@pytest.mark.parametrize('text', ['subscriber-secret', 'client-secret'])
async def test_secret_answer_still_deletes_known_thread(tmp_path, text):
    async with running(tmp_path, source=cli(tmp_path, text=text)) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries'})
        assert result.isError
        assert body(result)['error']['code'] == 'research_parse_error'
        assert body(result)['thread_deleted'] is True
        assert text not in json.dumps(body(result))
    assert len(calls(tmp_path)) == 2


async def test_verify_citations_uses_fetch_query_and_expandable_evidence(tmp_path):
    from vresto.cache import DocumentCache

    canonical = document()
    DocumentCache(tmp_path / 'cache').store(canonical)
    async with running(tmp_path, source=cli(tmp_path)) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries', 'verify_citations': True})
        data = body(result)
        quote = data['verified_citations'][0]['highlights'][0]
        assert quote['text'] == canonical['content']
        assert quote['evidence_id']
        expanded = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]}))
        assert expanded['sources'][0]['highlights'][0]['text'] == canonical['content']


async def test_verification_policy_failure_is_per_source(tmp_path):
    async with running(tmp_path, source=cli(tmp_path, reference_url='file:///etc/hosts')) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries', 'verify_citations': True})
        data = body(result)
        assert not result.isError
        assert data['verified_citations'][0]['error']['code'] == 'policy_rejected'


async def test_daily_cap_is_per_tier_and_failed_attempts_count(tmp_path):
    async with running(tmp_path, source=cli(tmp_path), research_daily_small=1,
                       research_model='configured') as (url, _), client(url) as session:
        assert not (await session.call_tool('research', {'query': 'first'})).isError
        capped = await session.call_tool('research', {'query': 'second'})
        assert body(capped)['error']['code'] == 'research_daily_cap'
        assert not (await session.call_tool('research', {'query': 'big', 'tier': 'big'})).isError
    assert len(calls(tmp_path)) == 4


async def test_research_pool_is_independent_and_cancellation_reaps_process(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path, action='time.sleep(30)')) as service:
        research = Research(service.discovery, service)

        async def progress(_):
            pass

        task = asyncio.create_task(research.run(ResearchInput(query='retries'), progress))
        try:
            async with asyncio.timeout(3):
                while not (tmp_path / 'calls.jsonl').exists():
                    await asyncio.sleep(0.01)
            assert not service.discovery.processes.admitted
            assert research.cli.processes.admitted == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not research.cli.processes.active
            assert not research.cli.processes.admitted
        finally:
            await research.cli.processes.close()


@pytest.mark.parametrize('settings,limit', [({}, 4), ({'research_concurrency': 2}, 2),
                                         ({'research_concurrency': 1}, 1),
                                         ({'research_concurrency': 16}, 16)])
async def test_research_concurrency_default_and_override(tmp_path, settings, limit):
    async with retrieval(tmp_path, source=cli(tmp_path), **settings) as service:
        research = Research(service.discovery, service)
        started = asyncio.Queue()
        release = asyncio.Event()

        async def progress(_):
            started.put_nowait(True)
            await release.wait()

        tasks = [asyncio.create_task(research.run(ResearchInput(query='retries'), progress))
                 for _ in range(limit + 1)]
        try:
            async with asyncio.timeout(3):
                for _ in range(limit):
                    await started.get()
                while not research.cli.processes.waiters:
                    await asyncio.sleep(0)
            assert started.empty()
            assert research.cli.processes.admitted == limit
            assert len(research.cli.processes.waiters) == 1
            assert not service.discovery.processes.admitted
            release.set()
            async with asyncio.timeout(10):
                results = await asyncio.gather(*tasks)
            assert all('answer' in result for result in results)
            assert not research.cli.processes.admitted
            assert not research.cli.processes.waiters
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await research.cli.processes.close()


async def test_answer_first_budget_bounds_multibyte_prose_references_and_quotes(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path), response_budget_bytes=4096) as service:
        research = Research(service.discovery, service)
        # The research operator budget is Settings-owned.
        from dataclasses import replace
        research.settings = replace(research.settings, response_budget_bytes=4096)
        data = {'answer': {'text': '漢' * 10000, 'generated': True}, 'tier': 'small',
                'references': [{'url': f'https://example.org/{i}', 'title': 'x' * 1000} for i in range(20)],
                'verified_citations': [{'highlights': [{'text': 'x' * 6000, 'evidence_id': 'id'}]}]}
        bounded = research.budget(data)
        assert wire_bytes(bounded) <= 4096
        assert bounded['answer']['response_truncated'] is True
        assert bounded['answer']['text']
        assert bounded['omitted_reference_count'] > 0
        assert bounded['verified_citations'][0]['highlights'] == []
        await research.cli.processes.close()


@pytest.mark.parametrize('arguments', [
    {'query': ' '}, {'query': 'x', 'tier': 'other'}, {'query': 'x', 'verify_citations': 1},
    {'query': 'x', 'model': 'arbitrary'}, {'query': 'subscriber-secret'},
])
async def test_research_invalid_input(tmp_path, arguments):
    async with running(tmp_path, source=cli(tmp_path)) as (url, _), client(url) as session:
        assert (await session.call_tool('research', arguments)).isError
    assert not (tmp_path / 'calls.jsonl').exists()


async def test_verify_citations_acquires_local_fixture_page(tmp_path):
    from aiohttp import web
    from test_fetch import site

    async def handler(request):
        assert 'subscriber-secret' not in str(request.headers)
        assert 'client-secret' not in str(request.headers)
        return web.Response(content_type='text/html', text=(
            '<title>Retry guide</title><main><h1>Retries</h1>'
            '<p>Retries require version 2. Never retry older versions.</p></main>'))

    async with site(handler) as (base, options):
        async with running(tmp_path, source=cli(tmp_path, reference_url=base + '/guide'),
                           acquisition_options=options) as (url, _), client(url) as session:
            result = await session.call_tool('research', {'query': 'retries', 'verify_citations': True})
            data = body(result)
            assert not result.isError
            source = data['verified_citations'][0]
            assert source['requested_url'] == base + '/guide'
            assert source['highlights'][0]['evidence_id']
            assert 'Never retry older versions.' in source['highlights'][0]['text']


@pytest.mark.parametrize('raw', [[], {'thread': [], 'message': []}, {'message': None},
                              {'message': {'markdown': 1}}, {'message': {'markdown': ''}}])
async def test_malformed_response_shapes_return_parse_error(tmp_path, raw):
    async with running(tmp_path, source=cli(tmp_path, action=f'print(json.dumps({raw!r}))')) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries'})
        assert body(result)['error']['code'] == 'research_parse_error'
        assert body(result)['thread_deleted'] is False


async def test_reference_echo_is_rejected_but_thread_is_deleted(tmp_path):
    async with running(tmp_path, source=cli(tmp_path, reference_url='https://example.org/%73ubscriber-secret')) as (url, _), client(url) as session:
        result = await session.call_tool('research', {'query': 'retries'})
        assert body(result)['error']['code'] == 'research_parse_error'
        assert body(result)['thread_deleted'] is True
        assert 'subscriber-secret' not in json.dumps(body(result))


async def test_research_queue_shares_request_deadline(tmp_path):
    async with retrieval(tmp_path, source=cli(tmp_path, action='time.sleep(30)'),
                         research_concurrency=1, research_timeout=1) as service:
        research = Research(service.discovery, service)

        async def progress(_):
            pass

        tasks = [asyncio.create_task(research.run(ResearchInput(query='retries'), progress))]
        try:
            async with asyncio.timeout(3):
                while not (tmp_path / 'calls.jsonl').exists():
                    await asyncio.sleep(0.01)
            tasks += [asyncio.create_task(research.run(ResearchInput(query='retries'), progress))
                      for _ in range(2)]
            await asyncio.sleep(0.01)
            full = await research.run(ResearchInput(query='retries'), progress)
            assert full['error']['code'] == 'research_timeout'
            for task in tasks[1:]:
                assert (await task)['error']['code'] == 'research_timeout'
            assert not service.discovery.processes.waiters
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await research.cli.processes.close()


async def test_verification_deadline_preserves_generated_answer(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    async with retrieval(tmp_path, source=cli(tmp_path), research_timeout=1) as service:
        research = Research(service.discovery, service)

        async def fetch(_):
            await asyncio.sleep(30)

        async def progress(_):
            pass

        monkeypatch.setattr(service, 'fetch', AsyncMock(side_effect=fetch))
        try:
            data = await research.run(ResearchInput(query='retries', verify_citations=True), progress)
            assert data['answer']['generated']
            assert data['thread_deleted']
            assert data['verification_error']['code'] == 'research_timeout'
            assert 'error' not in data
        finally:
            await research.cli.processes.close()


@pytest.mark.parametrize('settings', [
    {'research_timeout': 0}, {'research_timeout': 1801},
    {'research_concurrency': 0}, {'research_concurrency': 17},
    {'research_daily_small': -1}, {'research_daily_big': -1}, {'research_model': ' '},
])
def test_research_operator_limit_validation(settings):
    from vresto.server import Settings

    with pytest.raises(ValueError):
        Settings(access_token='client-secret', session_token='subscriber-secret', **settings)


async def test_verification_is_bounded_to_first_ten_references(tmp_path):
    from vresto.cache import DocumentCache

    cache = DocumentCache(tmp_path / 'cache')
    for i in range(11):
        cache.store(document(i))
    payload = {'thread': {'id': 'thread-123'}, 'message': {
        'markdown': 'Generated.',
        'references_markdown': '\n'.join(f'[^' + str(i) + f']: [Source {i}](https://example.org/{i})' for i in range(11))}}
    async with running(tmp_path, source=cli(tmp_path, action=f'print(json.dumps({payload!r}))')) as (url, _), client(url) as session:
        data = body(await session.call_tool('research', {'query': 'retries', 'verify_citations': True}))
        assert data['citation_verification_omitted'] == 1
        assert len(data['references']) == 11
        assert len(data['verified_citations']) == 10
        assert 'model' not in data
