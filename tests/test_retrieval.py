"""Workflow coverage without MCP/HTTP; the executable remains the CLI boundary."""
from contextlib import asynccontextmanager
import json
import sys

import pytest

from vresto.acquisition import Acquisition
from vresto.acquisition_policy import AcquisitionOptions
from vresto.cache import CachedAcquisition, DocumentCache
from vresto.contract import SearchRequest
from vresto.discovery import Discovery
from vresto.retrieval import Retrieval
from vresto.server import Settings
from test_http import cli_response


@asynccontextmanager
async def retrieval(tmp_path, *, source, acquisition_options=None, response_budget_bytes=65536,
                    **cache_settings):
    fixture = tmp_path / 'cli.py'
    fixture.write_text(source)
    settings = Settings(access_token='client-secret', session_token='subscriber-secret',
                        command=(sys.executable, str(fixture)), cache_directory=tmp_path / 'cache', **cache_settings)
    discovery = Discovery(settings)
    acquisition = Acquisition(acquisition_options or AcquisitionOptions(deadline=settings.timeout))
    cache = DocumentCache(settings.cache_directory, settings.cache_cap_bytes,
                          settings.cache_retention_seconds)
    secrets = (settings.access_token, settings.session_token)
    try:
        yield Retrieval(discovery, acquisition, CachedAcquisition(acquisition, cache, secrets), cache, secrets, response_budget_bytes)
    finally:
        await discovery.processes.close()
        await acquisition.close()


async def search(service, arguments):
    return await service.search(SearchRequest.model_validate({'quick_answer': False, **arguments}))


async def test_query_is_a_literal_argument_and_update_dates_disclosed(tmp_path):
    source = '''import json, sys
if "--version" in sys.argv:
    print("kagi 0.23.0")
else:
    assert sys.argv[1:] == ["--error-format", "json", "search", "--format", "json", "--limit", "3", "--", "--query ; $(oops)"]
    print(json.dumps({"data": [{"rank": 4, "url": "https://example.org/docs", "title": "Docs", "snippet": "Text"}]}))
'''
    from vresto.contract import SearchInput

    async with retrieval(tmp_path, source=source) as service:
        result = await service.search(SearchInput(query="--query ; $(oops)", num_results=3,
                                                 content_mode="discovery"))
        assert "error" not in result
        assert result["results"][0]["provider_rank"] == 4
        assert result["date_semantics"] == "provider_update_date_not_verified_publication"


@pytest.mark.parametrize("payload,failed,code", [
    ('not JSON subscriber-secret', False, "discovery_parse_error"),
    ('[' * 2000 + ']' * 2000, False, "discovery_parse_error"),
    ('{"wrong": []}', False, "discovery_parse_error"),
    ('{"data": [{"url": "file:///etc/passwd", "title": "Bad"}]}', False, "discovery_parse_error"),
    ('{"data": [{"url": "https://example.org", "title": 4}]}', False, "discovery_parse_error"),
    ('{"data": [{"url": "https://example.org", "title": "subscriber-secret"}]}', False, "discovery_parse_error"),
    ('{"code": "authentication_error", "message": "expired subscriber-secret", "http_status": 403}', True, "upstream_authentication"),
    ('{"code": "network_error", "http_status": 429, "message": "client-secret"}', True, "rate_limited"),
    ('{"code": "parse_error", "message": "subscriber-secret"}', True, "discovery_parse_error"),
    ('{"code": "network_error", "http_status": 503}', True, "discovery_failed"),
])
async def test_discovery_failures_are_clear_and_never_echo_credentials(tmp_path, caplog, payload, failed, code):
    async with retrieval(tmp_path, source=cli_response(payload, failed=failed)) as service:
        result = await search(service, {"query": "x"})
        assert "error" in result
        assert result["error"]["code"] == code
        assert "subscriber-secret" not in json.dumps(result) + caplog.text
        assert "client-secret" not in json.dumps(result) + caplog.text


async def test_missing_ranks_and_empty_results_are_not_invented(tmp_path):
    payload = '{"data": [{"t": 0, "rank": null, "url": "https://example.org", "title": "Docs", "snippet": "", "published": "yesterday"}]}'
    async with retrieval(tmp_path, source=cli_response(payload)) as service:
        data = (await search(service, {"query": "x", "content_mode": "discovery"}))
        assert data["results"][0]["provider_rank"] is None
        assert "published" not in data["results"][0]
        assert "provider_rank_unavailable" in data["warnings"]
    async with retrieval(tmp_path, source=cli_response('{"data": []}')) as service:
        data = (await search(service, {"query": "x"}))
        assert data["results"] == []
        assert "empty_results_may_indicate_upstream_markup_change" in data["warnings"]


@pytest.mark.parametrize('version', ['0.21.0', '0.22.0'])
async def test_unpinned_cli_is_rejected(tmp_path, version):
    source = f'''import sys
if "--version" in sys.argv:
    print("kagi {version}")
else:
    print('{{"data": []}}')
'''
    async with retrieval(tmp_path, source=source) as service:
        result = await search(service, {"query": "x"})
        assert "error" in result
        assert result["error"]["code"] == "discovery_cli_version"


async def test_cli_receives_only_upstream_session_without_host_auth_or_preferences(tmp_path):
    source = '''import json, os, sys
if "--version" in sys.argv:
    print("kagi 0.23.0")
else:
    assert os.environ["KAGI_SESSION_TOKEN"] == "subscriber-secret"
    assert not any(k in os.environ for k in ("KAGI_API_KEY", "KAGI_API_TOKEN", "VRESTO_ACCESS_TOKEN", "KAGI_BASE_URL"))
    assert all("client-secret" not in value for value in os.environ.values())
    assert "subscriber-secret" not in str(sys.argv)
    assert not os.path.exists(os.environ["KAGI_CONFIG"])
    assert os.environ["RUST_LOG"] == "off"
    print(json.dumps({"data": [{"t": 0, "rank": 1, "url": "https://example.org", "title": "Isolated", "snippet": ""}]}))
'''
    async with retrieval(tmp_path, source=source) as service:
        result = await search(service, {"query": "x", "content_mode": "discovery"})
        assert "error" not in result
        assert result["results"][0]["title"] == "Isolated"


@pytest.mark.parametrize('tool', ['search', 'fetch'])
@pytest.mark.parametrize('mode', ['full_text', 'highlights'])
async def test_acquired_workflow_screens_before_selection_without_mutating_canonical(tmp_path, monkeypatch, tool, mode):
    from copy import deepcopy
    import hashlib
    from unittest.mock import AsyncMock

    from vresto.budget import wire_bytes
    from vresto.contract import FetchRequest

    text = '# Retries\n\nRetries require version 2. Never retry version 1.\n'
    source = {'requested_url': 'https://example.org/docs', 'resolved_url': 'https://example.org/docs',
              'content': text, 'title': 'Retries', 'cache_state': 'hit',
              'content_version': hashlib.sha256(text.encode()).hexdigest(),
              'link_targets': [{'url': 'https://example.org/subscriber-secret'}],
              'links_truncated': True}
    contaminated = dict(source, content='subscriber-secret', requested_url='https://example.org/other')
    canonical = {'sources': [source, contaminated]}
    original = deepcopy(canonical)
    async with retrieval(tmp_path, source=cli_response('{"data": []}')) as service:
        fetch = AsyncMock(return_value=canonical)
        monkeypatch.setattr(service.cached_acquisition, 'fetch_many', fetch)
        monkeypatch.setattr(service.discovery, 'search', AsyncMock(return_value={
            'results': [{'url': s['requested_url'], 'provider_rank': i + 1} for i, s in enumerate(canonical['sources'])],
            'content_mode': 'discovery'}))
        arguments = {'query': 'retries', 'content_mode': mode, 'response_budget_bytes': 4096}
        if tool == 'fetch':
            arguments['urls'] = [s['requested_url'] for s in canonical['sources']]
            data = await service.fetch(FetchRequest.model_validate(arguments))
            sources = data['sources']
        else:
            data = await service.search(SearchRequest.model_validate(arguments))
            sources = data['results']
        assert fetch.await_count == 1
        assert sources[1] == {'error': {'code': 'fetch_failed'}}
        assert 'link_targets' not in sources[0]
        assert 'links_truncated' not in sources[0]
        assert 'subscriber-secret' not in json.dumps(data)
        assert data['acquisition_limits']['returned_page_limit'] == (10 if tool == 'fetch' else 20)
        assert data['budget']['failed_source_count'] == 1
        assert wire_bytes(data) <= 4096
        if mode == 'highlights':
            assert sources[0]['highlights'][0]['text'] == text
            assert 'content' not in sources[0]
        else:
            assert sources[0]['content'] == text
        assert canonical == original


async def test_normalized_query_terms_are_screened_after_highlight_selection(tmp_path):
    async with retrieval(tmp_path, source=cli_response('{"data": []}')) as service:
        data = await search(service, {'query': 'SUBSCRIBER-SECRET', 'content_mode': 'highlights'})
        assert data['error']['code'] == 'fetch_failed'
        assert 'subscriber-secret' not in json.dumps(data)


async def test_evidence_validation_returns_final_error_without_http(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from vresto.contract import FetchRequest

    async with retrieval(tmp_path, source=cli_response('{"data": []}')) as service:
        fetch = AsyncMock()
        monkeypatch.setattr(service.cached_acquisition, 'fetch_many', fetch)
        data = await service.fetch(FetchRequest(evidence_ids=['malformed']))
        assert data == {'error': {'code': 'invalid_input', 'message':
            'Invalid tool input or unsupported control; see tool input schema.'}}
        fetch.assert_not_awaited()


def test_failed_and_stale_sources_are_logged_with_their_urls(caplog):
    from vresto.retrieval import _log_failures
    caplog.set_level('WARNING', logger='vresto')
    _log_failures({'results': [
        {'url': 'https://www.reddit.com/r/x/comments/1/', 'error': {'code': 'blocked', 'reason': 'login_required'}},
        {'requested_url': 'https://nixos.wiki/a', 'error': {'code': 'blocked', 'reason': 'challenge_timeout',
                                                             'http_status': 403}},
        {'requested_url': 'https://ok.example/', 'content': 'fine'},
        {'requested_url': 'https://stale.example/', 'refresh_error': {'code': 'fetch_timeout'}},
        {'error': {'code': 'fetch_failed'}},  # credential-bearing source already stripped of its URL
    ]})
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        'source_failed url=https://www.reddit.com/r/x/comments/1/ code=blocked reason=login_required http_status=- stage=-',
        'source_failed url=https://nixos.wiki/a code=blocked reason=challenge_timeout http_status=403 stage=-',
        'source_stale url=https://stale.example/ code=fetch_timeout reason=- http_status=- stage=-',
        'source_failed url=- code=fetch_failed reason=- http_status=- stage=-',
    ]
