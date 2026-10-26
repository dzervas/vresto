"""Failure diagnostics stay correlated and useful without changing MCP payloads."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from vresto.acquisition_policy import FetchError
from vresto.browser import Browser
from vresto.contract import FetchInput, SearchInput
from test_http import body, cli_response, client, running
from test_fetch import site
from test_retrieval import retrieval
from test_summaries import document


QUERY = 'private question must not enter logs'
SENSITIVE = ['subscriber-secret', 'client-secret', QUERY, 'private-cli-body', 'private-exception-message']


def messages(caplog, prefix):
    return [record for record in caplog.records if record.name == 'vresto' and record.getMessage().startswith(prefix)]


def assert_safe(caplog):
    text = caplog.text + repr([record.args for record in caplog.records if record.name == 'vresto'])
    assert all(value not in text for value in SENSITIVE)
    assert all(record.exc_info is None and record.stack_info is None
               for record in caplog.records if record.name == 'vresto')


@pytest.fixture
def no_warmup(monkeypatch):
    monkeypatch.setattr('vresto.server.Acquisition.warmup', lambda _: None)


@pytest.mark.parametrize('status', [404, 410, 500, 503])
def test_http_error_keeps_private_status_without_changing_public_error(status):
    with pytest.raises(FetchError) as raised:
        Browser._check_status(SimpleNamespace(status=status))
    assert raised.value.code == 'fetch_failed'
    assert raised.value.details == {}
    assert raised.value.diagnostics == {'reason': 'http_status', 'http_status': status, 'stage': 'http_response'}


@pytest.mark.parametrize('status,code,details', [
    (401, 'blocked', {'reason': 'http_status', 'http_status': 401}),
    (403, 'blocked', {'reason': 'http_status', 'http_status': 403}),
    (429, 'rate_limited', {}),
])
def test_classified_http_errors_also_keep_private_status(status, code, details):
    with pytest.raises(FetchError) as raised:
        Browser._check_status(SimpleNamespace(status=status))
    assert raised.value.code == code and raised.value.details == details
    assert raised.value.diagnostics['http_status'] == status


@pytest.mark.parametrize('error,reason,upstream,status', [
    ({'code': 'network_error', 'message': 'network error: request to https://kagi.com/search?q=private timed out before Kagi responded. Try again.'}, 'upstream_timeout', 'network_error', '-'),
    ({'code': 'network_error', 'message': 'network error: could not connect to https://kagi.com/search. private-cli-body'}, 'upstream_connection', 'network_error', '-'),
    ({'code': 'network_error', 'http_status': 503}, 'http_status', 'network_error', '503'),
    ({'code': 'network_error', 'http_status': 429}, 'http_status', 'network_error', '429'),
    ({'code': 'authentication_error', 'http_status': 403}, 'http_status', 'authentication_error', '403'),
    ({'code': 'configuration_error'}, 'cli_error', 'configuration_error', '-'),
    ({'code': 'forged\nsubscriber-secret', 'http_status': 'client-secret'}, 'cli_error', 'unknown', '-'),
    ({'code': {'private-cli-body': QUERY}, 'http_status': True}, 'cli_error', 'unknown', '-'),
])
async def test_cli_exit_diagnostics_are_fixed_and_sanitized(tmp_path, caplog, error, reason, upstream, status):
    caplog.set_level(logging.INFO, logger='vresto')
    error = {**error, 'suggestions': [QUERY, 'subscriber-secret'], 'extra': 'private-cli-body'}
    error.setdefault('message', 'client-secret ' + QUERY)
    async with retrieval(tmp_path, source=cli_response(json.dumps(error), failed=True)) as service:
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
    assert 'error' in data
    records = messages(caplog, 'cli_failure ')
    assert len(records) == 1
    text = records[0].getMessage()
    for field in ['operation=search', 'phase=execute', 'reason=' + reason,
                  'upstream_code=' + upstream, 'http_status=' + status, 'exit_code=1', 'elapsed_ms=']:
        assert field in text
    assert_safe(caplog)


@pytest.mark.parametrize('payload', ['not JSON private-cli-body', '[]', 'null', '[' * 12000])
async def test_malformed_cli_stderr_is_identified_without_echoing_it(tmp_path, caplog, payload):
    caplog.set_level(logging.INFO, logger='vresto')
    async with retrieval(tmp_path, source=cli_response(payload, failed=True)) as service:
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
    assert data['error']['code'] == 'discovery_failed'
    assert len(messages(caplog, 'cli_failure ')) == 1
    assert 'reason=invalid_error_output' in messages(caplog, 'cli_failure ')[0].getMessage()
    assert_safe(caplog)


async def test_invalid_success_output_has_a_response_phase_diagnostic(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='vresto')
    async with retrieval(tmp_path, source=cli_response('private-cli-body')) as service:
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
    assert data['error']['code'] == 'discovery_parse_error'
    assert 'phase=response' in messages(caplog, 'cli_failure ')[0].getMessage()
    assert_safe(caplog)


async def test_missing_cli_reports_spawn_type_without_command_path(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='vresto')
    async with retrieval(tmp_path, source=cli_response('{}')) as service:
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=FileNotFoundError('private-exception-message')))
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
    assert data['error']['code'] == 'discovery_failed'
    text = messages(caplog, 'cli_failure ')[0].getMessage()
    assert 'phase=version' in text and 'process_phase=spawn' in text
    assert 'type=FileNotFoundError' in text and 'reason=os_error' in text
    assert_safe(caplog)


async def test_service_deadline_is_distinct_from_upstream_timeout(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='vresto')
    source = "import time\ntime.sleep(5)\n"
    async with retrieval(tmp_path, source=source, timeout=.1) as service:
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
        assert not service.discovery.processes.active
    assert data['error']['code'] == 'discovery_timeout'
    assert any('reason=service_deadline' in record.getMessage() for record in messages(caplog, 'cli_failure '))
    assert_safe(caplog)


async def test_concurrent_calls_correlate_cli_failure_and_tool_summary(tmp_path, caplog, no_warmup):
    caplog.set_level(logging.INFO, logger='vresto')
    source = cli_response(json.dumps({'code': 'network_error', 'http_status': 503,
                                     'message': QUERY + ' subscriber-secret'}), failed=True)
    async with running(tmp_path, source=source) as (url, _), client(url) as session:
        results = await asyncio.gather(*(session.call_tool('search', {'query': QUERY, 'quick_answer': False}) for _ in range(2)))
    assert all(body(result)['error']['code'] == 'discovery_failed' for result in results)
    summaries = messages(caplog, 'tool=search ')
    assert len(summaries) == 2
    identifiers = {record.request_id for record in summaries}
    assert len(identifiers) == 2 and all(re.fullmatch('[0-9a-f]{16}', value) for value in identifiers)
    for identifier in identifiers:
        assert len([record for record in messages(caplog, 'cli_failure ') if record.request_id == identifier]) == 1
    assert_safe(caplog)


async def test_request_context_resets_and_background_logs_do_not_keep_old_id(caplog):
    from vresto.diagnostics import request_logging

    caplog.set_level(logging.INFO, logger='vresto')
    release = asyncio.Event()

    async def background():
        await release.wait()
        logging.getLogger('vresto').info('background_probe')

    with request_logging():
        logging.getLogger('vresto').info('request_probe')
        task = asyncio.create_task(background())
    logging.getLogger('vresto').info('outside_probe')
    release.set()
    await task
    assert re.fullmatch('[0-9a-f]{16}', messages(caplog, 'request_probe')[0].request_id)
    assert messages(caplog, 'outside_probe')[0].request_id == '-'
    assert messages(caplog, 'background_probe')[0].request_id == '-'


async def test_http_source_diagnostic_correlates_and_stays_off_wire(tmp_path, caplog, monkeypatch, no_warmup):
    from vresto.cache import CachedAcquisition

    caplog.set_level(logging.INFO, logger='vresto')
    failure = {'requested_url': 'https://example.org/missing', 'error': {'code': 'fetch_failed'},
               '_diagnostic': {'reason': 'http_status', 'http_status': 404, 'stage': 'http_response'}}
    monkeypatch.setattr(CachedAcquisition, 'fetch_many', AsyncMock(return_value={'sources': [failure]}))
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': ['https://example.org/missing']})
    data = body(result)
    assert data['sources'] == [{'requested_url': 'https://example.org/missing', 'error': {'code': 'fetch_failed'}}]
    assert '_diagnostic' not in result.model_dump_json()
    source_log = messages(caplog, 'source_failed ')[0]
    summary = messages(caplog, 'tool=fetch ')[0]
    assert source_log.request_id == summary.request_id != '-'
    assert 'http_status=404' in source_log.getMessage() and 'stage=http_response' in source_log.getMessage()
    assert 'outcome=partial' in summary.getMessage() and 'failed_sources=1' in summary.getMessage()


async def test_stale_refresh_preserves_diagnostic_only_until_output_logging(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='vresto')
    async with retrieval(tmp_path, source=cli_response('{}')) as service:
        cached = document()
        cached['fetched_at'] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        service.cache.store(cached)
        failure = {'requested_url': cached['requested_url'], 'error': {'code': 'fetch_failed'},
                   '_diagnostic': {'reason': 'http_status', 'http_status': 503, 'stage': 'http_response'}}
        monkeypatch.setattr(service.acquisition, 'fetch_many', AsyncMock(return_value={'sources': [failure]}))
        data = await service.fetch(FetchInput(urls=[cached['requested_url']]))
        assert data['sources'][0]['refresh_error'] == {'code': 'fetch_failed'}
        assert data['sources'][0]['cache_state'] == 'stale'
        assert '_diagnostic' not in json.dumps(data)
        assert '_diagnostic' not in json.dumps(service.cache.lookup(cached['requested_url']))
    text = messages(caplog, 'source_stale ')[0].getMessage()
    assert 'http_status=503' in text and 'reason=http_status' in text


async def test_quick_answer_failure_is_a_partial_tool_result(tmp_path, caplog, no_warmup):
    caplog.set_level(logging.INFO, logger='vresto')
    async with running(tmp_path, source=cli_response('{"data": []}')) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': QUERY})
    assert not result.isError and body(result)['quick_answer_error']['code'] == 'discovery_parse_error'
    summary = messages(caplog, 'tool=search ')[0]
    assert 'outcome=partial' in summary.getMessage() and 'supplementary_errors=1' in summary.getMessage()
    assert messages(caplog, 'cli_failure ')[0].request_id == summary.request_id
    assert_safe(caplog)


async def test_contaminated_source_is_screened_before_logging(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='vresto')
    async with retrieval(tmp_path, source=cli_response('{}')) as service:
        failure = {'requested_url': 'https://example.org/subscriber-secret', 'error': {'code': 'fetch_failed'},
                   '_diagnostic': {'http_status': 404, 'reason': 'http_status', 'stage': 'http_response'}}
        monkeypatch.setattr(service.cached_acquisition, 'fetch_many', AsyncMock(return_value={'sources': [failure]}))
        data = await service.fetch(FetchInput(urls=['https://example.org/public']))
    assert data['sources'] == [{'error': {'code': 'fetch_failed'}}]
    assert 'url=-' in messages(caplog, 'source_failed ')[0].getMessage()
    assert_safe(caplog)


async def test_version_failure_does_not_log_version_output(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='vresto')
    async with retrieval(tmp_path, source="print('private-cli-body')") as service:
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
    assert data['error']['code'] == 'discovery_cli_version'
    text = messages(caplog, 'cli_failure ')[0].getMessage()
    assert 'phase=version' in text and 'reason=version_mismatch' in text and 'exit_code=0' in text
    assert_safe(caplog)


async def test_signal_exit_is_not_reported_as_an_http_or_service_timeout(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='vresto')
    source = "import os,signal,sys\nif '--version' in sys.argv: print('kagi 0.23.0')\nelse: os.kill(os.getpid(),signal.SIGKILL)\n"
    async with retrieval(tmp_path, source=source) as service:
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
    assert data['error']['code'] == 'discovery_failed'
    text = messages(caplog, 'cli_failure ')[0].getMessage()
    assert 'reason=signal' in text and 'exit_code=-9' in text and 'http_status=-' in text
    assert_safe(caplog)


async def test_admission_timeout_is_reported_before_any_cli_spawn(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='vresto')
    async with retrieval(tmp_path, source=cli_response('{}'), timeout=.1) as service:
        processes = service.discovery.processes
        async with processes.admission(), processes.admission():
            data = await service.search(SearchInput(query=QUERY, quick_answer=False))
            assert not processes.active
        assert processes.admitted == 0 and not processes.waiters
    assert data['error']['code'] == 'discovery_timeout'
    text = messages(caplog, 'cli_failure ')[0].getMessage()
    assert 'phase=admission' in text and 'reason=service_deadline' in text
    assert 'admitted=2' in text
    assert_safe(caplog)


async def test_diagnostic_handler_failure_does_not_replace_original_cli_error(tmp_path, monkeypatch):
    async with retrieval(tmp_path, source=cli_response('{"http_status":503}', failed=True)) as service:
        monkeypatch.setattr('vresto.discovery.logger.warning', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('private-exception-message')))
        data = await service.search(SearchInput(query=QUERY, quick_answer=False))
    assert data['error']['code'] == 'discovery_failed'


async def test_stale_and_omitted_failure_counts_are_visible_in_tool_summary(tmp_path, caplog, monkeypatch, no_warmup):
    from vresto.retrieval import Retrieval

    caplog.set_level(logging.INFO, logger='vresto')
    data = {'sources': [{'requested_url': 'https://example.org/docs', 'content': 'Public text',
                         'stale': True, 'refresh_error': {'code': 'fetch_timeout'}}],
            'budget': {'failed_source_count': 2}}
    monkeypatch.setattr(Retrieval, 'fetch', AsyncMock(return_value=data))
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': ['https://example.org/docs']})
    assert not result.isError
    text = messages(caplog, 'tool=fetch ')[0].getMessage()
    assert 'outcome=partial' in text and 'failed_sources=2' in text and 'stale_sources=1' in text


@pytest.mark.parametrize('status', [404, 503])
async def test_real_http_failure_reaches_sanitized_source_log(tmp_path, caplog, status):
    caplog.set_level(logging.INFO, logger='vresto')

    async def handler(request):
        return web.Response(status=status, content_type='text/html', text='<h1>Not available</h1>')

    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'})
    assert body(result)['sources'][0]['error'] == {'code': 'fetch_failed'}
    assert '_diagnostic' not in result.model_dump_json()
    source_log = messages(caplog, 'source_failed ')[0]
    assert f'http_status={status}' in source_log.getMessage()
    assert 'stage=http_response' in source_log.getMessage()
    assert source_log.request_id == messages(caplog, 'tool=fetch ')[0].request_id != '-'
    assert_safe(caplog)


def test_cache_store_never_persists_private_diagnostics(tmp_path):
    from vresto.cache import DocumentCache

    cache = DocumentCache(tmp_path)
    source = document()
    source['_diagnostic'] = {'http_status': 503, 'stage': 'http_response'}
    cache.store(source)
    assert '_diagnostic' not in cache.lookup(source['requested_url'])
    assert '_diagnostic' in source
