"""Adapter precedence, privacy, fail-soft disclosure and generic equivalence."""
import asyncio
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from vresto import sites
from vresto.extraction import Extracted, extract
from vresto.sites.github import ADAPTER


@pytest.fixture
def coverage():
    return {'exhaustive': False, 'partial': False, 'actions': 0,
            'policy': 'bounded_read_only_in_page_controls', 'stop_reason': 'settled'}


@pytest.mark.parametrize('raises', [False, True])
def test_extraction_fallback_is_identical_and_disclosed(monkeypatch, coverage, caplog, raises):
    document = '<main><h1>Example</h1><p>Warning: preserve qualified prose.</p></main>'
    expected = extract(document, 'https://example.com/')
    def failing(document, url):
        if raises:
            raise RuntimeError('SECRET https://private.invalid/')
        return None
    adapter = sites.SiteAdapter('fragile', lambda url: True, extract=failing)
    monkeypatch.setattr(sites, 'ADAPTERS', (adapter,))
    assert sites.extract_document(document, 'https://example.com/', coverage=coverage) == expected
    assert coverage['site_adapter'] == {'name': 'fragile', 'outcome': 'fallback'}
    assert 'SECRET' not in caplog.text and 'private.invalid' not in caplog.text


def test_registry_first_match_wins(monkeypatch, coverage):
    first = sites.SiteAdapter('first', lambda url: True,
                              extract=lambda document, url: Extracted('first', [], False))
    second = replace(first, name='second')
    monkeypatch.setattr(sites, 'ADAPTERS', (first, second))
    assert sites.select('https://example.com/') is first
    assert sites.extract_document('<p>generic</p>', 'https://example.com/', coverage=coverage).markdown == 'first'
    assert coverage['site_adapter'] == {'name': 'first', 'outcome': 'applied'}


def test_generic_is_default_and_github_match_is_exact(coverage):
    assert ADAPTER.matches(urlsplit('https://github.com/org/repo/issues/1'))
    assert not ADAPTER.matches(urlsplit('https://github.com.evil.invalid/'))
    assert sites.select('https://example.com/') is None
    sites.extract_document('<p>Generic</p>', 'https://example.com/', coverage=coverage)
    assert 'site_adapter' not in coverage


def test_cancellation_is_not_swallowed(monkeypatch, coverage):
    def cancelled(document, url):
        raise asyncio.CancelledError
    monkeypatch.setattr(sites, 'ADAPTERS', (sites.SiteAdapter('cancelled', lambda url: True, extract=cancelled),))
    with pytest.raises(asyncio.CancelledError):
        sites.extract_document('<p>generic</p>', 'https://example.com/', coverage=coverage)


@pytest.mark.parametrize('raises', [False, True])
async def test_expansion_hook_falls_back_before_actions(monkeypatch, raises):
    def failing():
        if raises:
            raise ValueError('private')
        return None
    adapter = sites.SiteAdapter('fragile', lambda url: True, expansion=failing)
    monkeypatch.setattr(sites, 'ADAPTERS', (adapter,))
    async def expanded(page, options, **kwargs):
        assert kwargs['scope_selector'] == sites.Expansion().scope_selector
        assert kwargs['deduplicate'] == sites.Expansion().deduplicate
        return {'exhaustive': False, 'partial': False, 'actions': 0,
                'policy': 'bounded_read_only_in_page_controls', 'stop_reason': 'settled'}
    monkeypatch.setattr(sites, 'expand_generic', expanded)
    class Page:
        url = 'https://example.com/'
    coverage = await sites.expand_content(Page(), None, deadline=1, revalidate=None)
    assert coverage['site_adapter'] == {'name': 'fragile', 'outcome': 'fallback'}


def test_phase_fallback_is_not_overwritten(monkeypatch, coverage):
    coverage['site_adapter'] = {'name': 'fragile', 'outcome': 'fallback'}
    monkeypatch.setattr(sites, 'ADAPTERS', (sites.SiteAdapter('fragile', lambda url: True,
        extract=lambda document, url: Extracted('specialized', [], False)),))
    result = sites.extract_document('<p>generic</p>', 'https://example.com/', coverage=coverage)
    assert result.markdown == 'generic\n'
    assert coverage['site_adapter']['outcome'] == 'fallback'


@pytest.mark.parametrize('status,code,reason', [
    ('blocked', 'blocked', 'login_required'),
    ('unavailable', 'fetch_failed', 'content_unavailable'),
])
@pytest.mark.parametrize('expansion_fallback', [False, True])
def test_classified_failures_never_fall_back(monkeypatch, coverage, status, code, reason,
                                            expansion_fallback):
    from vresto.acquisition_policy import FetchError
    from vresto.sites.base import ExtractionOutcome
    adapter = sites.SiteAdapter('classified', lambda url: True,
        extract=lambda document, url: ExtractionOutcome(status))
    monkeypatch.setattr(sites, 'ADAPTERS', (adapter,))
    if expansion_fallback:
        coverage['site_adapter'] = {'name': 'classified', 'outcome': 'fallback'}
    with pytest.raises(FetchError) as error:
        sites.extract_document('<p>Wall text is not content.</p>', 'https://example.com/',
                               coverage=coverage)
    assert error.value.code == code
    assert error.value.details == {'reason': reason}


def test_partial_outcome_preserves_content_and_discloses_reason(monkeypatch, coverage):
    from vresto.sites.base import ExtractionOutcome
    content = Extracted('A qualified preview.', ['https://example.com/'], False)
    monkeypatch.setattr(sites, 'ADAPTERS', (sites.SiteAdapter('preview', lambda url: True,
        extract=lambda document, url: ExtractionOutcome('partial', content)),))
    assert sites.extract_document('', 'https://example.com/', coverage=coverage) is content
    assert coverage['partial'] is True
    assert coverage['site_adapter'] == {'name': 'preview', 'outcome': 'applied',
                                       'reason': 'public_preview'}


@pytest.mark.parametrize('status,code,reason', [
    ('blocked', 'blocked', 'login_required'),
    ('unavailable', 'fetch_failed', 'content_unavailable'),
])
async def test_outcomes_through_browser_and_cache(tmp_path, monkeypatch, status, code, reason):
    from aiohttp import web
    from test_fetch import site
    from test_http import body
    from retrieval_helpers import client, running
    from vresto.sites.base import ExtractionOutcome

    wall = False
    def extract_page(document, url):
        return ExtractionOutcome(status) if 'WALL_MARKER' in document else extract(document, url)
    monkeypatch.setattr(sites, 'ADAPTERS', (sites.SiteAdapter('classified', lambda url: True,
                                                           extract=extract_page),))
    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<main><h1>WALL_MARKER</h1></main>' if wall else
            '<main><h1>Original public content</h1><p>Keep this qualification.</p></main>'))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        async def fetch(**kwargs):
            return body(await session.call_tool('fetch', {'urls': [base], **kwargs}))['sources'][0]
        wall = True
        failed = await fetch(freshness='fresh_only')
        assert failed['error'] == {'code': code, 'reason': reason}
        assert (await fetch(freshness='cache_only'))['error']['code'] == 'cache_miss'
        wall = False
        original = await fetch(freshness='fresh_only')
        assert 'Original public content' in original['content']
        wall = True
        stale = await fetch(max_age_seconds=0)
        assert stale['stale'] is True
        assert stale['content'] == original['content']
        assert stale['refresh_error'] == {'code': code, 'reason': reason}
        assert (await fetch(freshness='fresh_only'))['error'] == {'code': code, 'reason': reason}
        assert (await fetch(freshness='cache_only'))['content'] == original['content']
