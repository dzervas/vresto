import json
from dataclasses import replace

from test_http import running, client, body, cli_response
from test_fetch import site
from aiohttp import web
import pytest


async def test_discovery_shared_budget_includes_large_metadata_and_single_structured_copy(tmp_path):
    payload = {'data': [{'url': f'https://example.org/{i}', 'title': 'Technical title ' * 200,
                         'snippet': 'Qualification: do not enable legacy mode. ' * 200,
                         'rank': i + 1} for i in range(10)]}
    async with running(tmp_path, max_bytes=200000, source=cli_response(json.dumps(payload))) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': 'docs', 'content_mode': 'discovery', 'response_budget_bytes': 4096})
        assert not result.isError
        data = body(result)
        assert data == result.structuredContent
        assert len(result.model_dump_json(exclude_none=True).encode()) <= 4096
        assert data['budget']['partial_coverage']
        assert data['budget']['source_count'] == 10
        assert data['budget']['omitted_source_indices'] or any(r.get('truncated_fields') for r in data['results'])
        assert all(r['url'].startswith('https://example.org/') for r in data['results'])


@pytest.mark.parametrize('limit', [4096, 16384, 65536, 262144])
async def test_full_text_budget_preserves_ten_source_outcomes_and_reports_truncation(tmp_path, limit):
    async def handler(request):
        if request.path == '/missing':
            return web.Response(status=404)
        return web.Response(content_type='text/html', text='<title>Technical guide</title><h1>Warning</h1><p>'
                            + 'Version 2: do not enable legacy mode. 漢字 \\"quoted\\". ' * 300 + '</p>')
    async with site(handler) as (base, options):
        # Ten same-origin pages are intentional budget inputs, not a queue-load test.
        options = replace(options, queue_capacity=10, queue_wait=30)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
            result = await session.call_tool('fetch', {'urls': [base + f'/{i}' for i in range(9)] + [base + '/missing'],
                                                     'response_budget_bytes': limit})
            assert not result.isError
            data = body(result)
            assert data == result.structuredContent
            size = len(result.model_dump_json(exclude_none=True).encode())
            print(f'full_text requested={limit} wire_bytes={size} returned={len(data["sources"])}')
            assert size <= limit
            if limit >= 16384:
                assert len(data['sources']) == 10
            assert data['sources'][-1]['error']['code'] == 'fetch_failed'
            assert all('error' not in s for s in data['sources'][:-1]), data
            if limit < 262144:
                assert all('content' in s['truncated_fields'] for s in data['sources'][:-1])
            assert data['budget']['partial_coverage']
            if limit >= 16384:
                assert data['budget']['omitted_source_indices'] == []
            assert data['budget']['failed_source_count'] == 1


@pytest.mark.parametrize('limit', [4095, 262145, True, '4096'])
async def test_invalid_response_budgets_are_rejected(tmp_path, limit):
    async with running(tmp_path) as (url, _), client(url) as session:
        for tool, arguments in [('search', {'query': 'docs'}), ('fetch', {'urls': ['https://example.org']})]:
            result = await session.call_tool(tool, {**arguments, 'response_budget_bytes': limit})
            assert result.isError
            assert body(result)['error']['code'] == 'invalid_input'


async def test_untrimmable_metadata_is_omitted_explicitly_not_corrupted(tmp_path):
    payload = {'data': [{'url': 'https://example.org/' + 'a' * 10000, 'title': 'Long URL', 'rank': 1},
                        {'url': 'https://example.org/useful', 'title': 'Useful', 'rank': 2}]}
    async with running(tmp_path, source=cli_response(json.dumps(payload))) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': 'docs', 'content_mode': 'discovery', 'response_budget_bytes': 4096})
        data = body(result)
        assert not result.isError
        assert [s['url'] for s in data['results']] == ['https://example.org/useful']
        assert data['budget']['omitted_source_indices'] == [0]
        assert data['budget']['returned_source_count'] == 1
        assert data['budget']['partial_coverage']
        assert len(result.model_dump_json(exclude_none=True).encode()) <= 4096


async def test_operator_default_is_advertised_and_used_with_client_override(tmp_path):
    async with running(tmp_path, response_budget_bytes=8192) as (url, _), client(url) as session:
        assert all(t.inputSchema['properties']['response_budget_bytes']['default'] == 8192
                   for t in (await session.list_tools()).tools)
        default = body(await session.call_tool('search', {'query': 'docs', 'content_mode': 'discovery'}))
        override = body(await session.call_tool('search', {'query': 'docs', 'content_mode': 'discovery', 'response_budget_bytes': 4096}))
        assert default['budget']['limit_bytes'] == 8192
        assert override['budget']['limit_bytes'] == 4096


@pytest.mark.parametrize('limit', [None, 4096, 262144])
async def test_representative_discovery_budget_measurements_and_boundaries(tmp_path, limit):
    async with running(tmp_path) as (url, _), client(url) as session:
        arguments = {'query': 'docs', 'content_mode': 'discovery'}
        if limit is not None:
            arguments['response_budget_bytes'] = limit
        result = await session.call_tool('search', arguments)
        data = body(result)
        size = len(result.model_dump_json(exclude_none=True).encode())
        print(f'discovery requested={limit} wire_bytes={size}')
        assert not result.isError
        assert len(data['results']) == 10
        assert not data['budget']['partial_coverage']
        assert size <= (limit or 65536)
