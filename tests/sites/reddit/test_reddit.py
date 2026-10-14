"""Real logged-out wall fixtures; no fabricated Reddit thread extraction."""
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from kagi_crawler import sites
from kagi_crawler.extraction import extract
from kagi_crawler.acquisition_policy import FetchError
from kagi_crawler.sites.reddit import ADAPTER
from kagi_crawler.types import Coverage

FIXTURES = Path(__file__).parent


@pytest.mark.parametrize('host', ['reddit.com', 'www.reddit.com', 'old.reddit.com'])
def test_matches_public_hosts(host):
    assert ADAPTER.matches(urlsplit(f'https://{host}/r/IAmA/comments/z1c9z/'))


@pytest.mark.parametrize('host', ['notreddit.com', 'www.reddit.com.example.org', 'oauth.reddit.com'])
def test_does_not_match_other_hosts(host):
    assert not ADAPTER.matches(urlsplit(f'https://{host}/r/IAmA/comments/z1c9z/'))


@pytest.mark.parametrize(('name', 'wall'), [
    ('www', 'Prove your humanity'),
    ('old', 'Log in to use old Reddit'),
    ('json', "You've been blocked by network security"),
])
def test_real_wall_is_blocked(monkeypatch, name, wall):
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    document = (FIXTURES / f'{name}.html').read_text()
    url = (FIXTURES / f'{name}.url').read_text().strip()
    coverage: Coverage = {'exhaustive': False, 'partial': False, 'actions': 0,
                          'stop_reason': 'settled', 'policy': 'bounded_read_only_in_page_controls'}
    assert wall in document
    with pytest.raises(FetchError) as error:
        sites.extract_document(document, url, coverage=coverage)
    assert error.value.code == 'blocked'
    assert error.value.details == {'reason': 'login_required'}


def test_unrecognized_markup_still_declines(monkeypatch):
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    document = (FIXTURES / 'old.html').read_text()
    assert '<shreddit-app' in document
    document = '<main><p>Unrecognized public page.</p></main>'
    url = (FIXTURES / 'old.url').read_text().strip()
    coverage: Coverage = {'exhaustive': False, 'partial': False, 'actions': 0,
                          'stop_reason': 'settled', 'policy': 'bounded_read_only_in_page_controls'}
    result = sites.extract_document(document, url, coverage=coverage)
    assert result == extract(document, url)
    assert coverage.get('site_adapter') == {'name': 'reddit', 'outcome': 'fallback'}


def test_wall_words_in_prose_and_scripts_are_not_classified():
    document = '''<main><h1>Discussing Reddit access</h1>
    <p>Prove your humanity. Log in to use old Reddit.
    You've been blocked by network security.</p>
    <script>"<h1>Prove your humanity</h1>"</script></main>'''
    assert ADAPTER.extract(document, 'https://www.reddit.com/r/example/') is None
