"""Public MCP contract: operational controls are not model-facing inputs."""
from dataclasses import replace

from aiohttp import web
import pytest
from pydantic import ValidationError

from vresto.acquisition_policy import AcquisitionOptions
from vresto.contract import CrawlInput, FetchInput, SearchInput
from vresto.server import Settings
from test_fetch import site
from test_http import body, client, running


@pytest.mark.parametrize('extra', [
    {'region': 'gb'}, {'lens': 2}, {'time': 'day'},
    {'from_date': '2026-01-01'}, {'to_date': '2026-02-01'},
    {'order': 'recency'}, {'verbatim': True}, {'personalized': False},
    {'max_age_seconds': 3600}, {'response_budget_bytes': 4096},
    {'freshness': 'refresh'}, {'freshness': 'cache_only'},
])
async def test_search_rejects_removed_controls(tmp_path, extra):
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': 'docs', **extra})
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'


@pytest.mark.parametrize('tool,arguments', [
    ('fetch', {'urls': ['https://example.org'], 'max_age_seconds': 3600}),
    ('fetch', {'urls': ['https://example.org'], 'freshness': 'cache_only'}),
    ('fetch', {'urls': ['https://example.org'], 'freshness': 'refresh'}),
    ('fetch', {'evidence_ids': ['bad']}),
    ('fetch', {'urls': ['https://example.org'], 'traversal': {}}),
    ('expand', {'evidence_ids': ['bad'], 'response_budget_bytes': 4096}),
    ('crawl', {'url': 'https://example.org', 'path_prefix': '/', 'max_depth': 2}),
])
async def test_removed_controls_are_rejected(tmp_path, tool, arguments):
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool(tool, arguments)
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'


@pytest.mark.parametrize('arguments', [
    {'url': 'file:///tmp/doc', 'path_prefix': '/'},
    {'url': 'https://example.org', 'path_prefix': '//docs'},
    {'url': 'https://user:pass@example.org', 'path_prefix': '/'},
    {'url': 'https://example.org', 'path_prefix': 'docs'},
    {'url': 'https://example.org', 'path_prefix': '/', 'max_pages': 21},
    {'url': 'https://example.org', 'path_prefix': '/', 'max_pages': True},
])
def test_crawl_scope_validation(arguments):
    with pytest.raises(ValidationError):
        CrawlInput.model_validate(arguments)


def test_public_defaults_and_deadline_bounds():
    assert SearchInput(query='docs').num_results == 10
    assert SearchInput(query='docs').content_mode == 'highlights'
    assert FetchInput(urls=['https://example.org']).content_mode is None
    crawl = CrawlInput(url='https://example.org:8443/docs/seed?q=1', path_prefix='/docs')
    scope = crawl.traversal()
    assert scope.origin == 'https://example.org:8443'
    assert (scope.max_pages, scope.max_depth, scope.max_seconds, scope.max_frontier) == (5, 1, 15, 32)
    assert scope.max_bytes == 8_388_608
    assert AcquisitionOptions().deadline == Settings.timeout == 120
    assert replace(AcquisitionOptions(), deadline=600).deadline == 600
    with pytest.raises(ValueError):
        replace(AcquisitionOptions(), deadline=600.01)


async def test_crawl_derives_origin_and_discloses_fixed_limits(tmp_path):
    seen = []

    async def handler(request):
        seen.append(request.path)
        return web.Response(content_type='text/html', text='''<h1>Docs</h1>
            <p>Public documentation with safety requirements and compatibility guidance.</p>
            <a href="/docs/next">Next</a><a href="/outside">Outside</a>''')

    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        result = await session.call_tool('crawl', {'url': base + '/docs', 'path_prefix': '/docs'})
        assert not result.isError, body(result)
        data = body(result)
        assert [s['requested_url'] for s in data['sources']] == [base + '/docs', base + '/docs/next']
        assert seen == ['/docs', '/docs/next']
        assert data['traversal']['omissions']['scope_rejected'] >= 1
        assert data['acquisition_limits']['returned_page_limit'] == 5
        assert data['content_mode'] == 'full_text'
