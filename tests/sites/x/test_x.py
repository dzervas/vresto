"""Real headed logged-out snapshots captured 2026-10-14; no account sessions."""
from pathlib import Path
from urllib.parse import urlsplit

from lxml import html
import pytest

from kagi_crawler import sites
from kagi_crawler.extraction import extract
from kagi_crawler.sites.x import ADAPTER
from kagi_crawler.acquisition_policy import FetchError

FIXTURES = Path(__file__).parent
SUPPORTED = ('jack-first-post', 'twitter-first-post', 'obama-post', 'musk-post',
             'naval-thread', 'nasa-profile', 'twitter-profile')


def fixture(name):
    return (FIXTURES / f'{name}.html').read_text(), (FIXTURES / f'{name}.url').read_text().strip()


def coverage():
    return {'exhaustive': False, 'partial': False, 'actions': 0,
            'policy': 'bounded_read_only_in_page_controls', 'stop_reason': 'settled'}


@pytest.mark.parametrize('name', SUPPORTED)
def test_captured_preview_goldens(monkeypatch, name):
    document, url = fixture(name)
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    disclosure = coverage()
    result = sites.extract_document(document, url, coverage=disclosure)
    assert result.markdown == (FIXTURES / f'{name}.md').read_text()
    assert disclosure['site_adapter'] == {'name': 'x', 'outcome': 'applied', 'reason': 'public_preview'}
    assert disclosure['partial'] is True
    assert 'not an exhaustive conversation or timeline' in result.markdown
    assert 'Continue with Google' not in result.markdown
    assert 'Did someone say' not in result.markdown
    assert 'Relevant people' not in result.markdown


@pytest.mark.parametrize('host', ['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'])
def test_exact_public_hosts(host):
    assert ADAPTER.matches(urlsplit(f'https://{host}/jack/status/20'))


@pytest.mark.parametrize('host', ['x.com.evil.invalid', 'twitter.com.evil.invalid',
                                 'notx.com', 'api.x.com', 'example.com'])
def test_host_lookalikes_declined(host):
    assert not ADAPTER.matches(urlsplit(f'https://{host}/jack/status/20'))


def test_thread_quotes_and_reply_qualifications():
    document, url = fixture('naval-thread')
    result = sites.extract_document(document, url)
    assert result is not None
    assert result.markdown.count('\n## ') == 43
    assert '## Quoted post: Naval (@naval) — Mar 17, 2019' in result.markdown
    assert "I'll remove immediately if you prefer it not be read this way." in result.markdown
    assert 'How to Get Rich (without getting lucky).pdf' in result.markdown
    assert 'https://t.co/A68G7AAQ0r' in result.links


def test_profile_identity_dates_and_counts():
    document, url = fixture('nasa-profile')
    result = sites.extract_document(document, url)
    assert result is not None
    for text in ('Making the seemingly impossible, possible.', 'Born October 1, 1958',
                 'Joined December 2007', '92.3M Followers', '## NASA (@NASA) — 6h',
                 'View count: 249K', 'Open broadcast:'):
        assert text in result.markdown


@pytest.mark.parametrize('mutation', ['articles', 'authors', 'dates', 'bodies', 'profile'])
def test_markup_drift_falls_back_with_disclosure(monkeypatch, mutation):
    document, url = fixture('nasa-profile' if mutation == 'profile' else 'jack-first-post')
    root = html.document_fromstring(document)
    if mutation == 'articles':
        for element in root.xpath('//article'):
            element.tag = 'div'
    elif mutation == 'authors':
        for element in root.xpath('//article//a[starts-with(string(.), "@")]'):
            element.text = 'identity unavailable'
            for child in list(element):
                element.remove(child)
    elif mutation == 'dates':
        for element in root.xpath('//article//a[contains(@href, "/status/")]'):
            element.set('href', '/unknown')
    elif mutation == 'bodies':
        for element in root.xpath('//article//div[contains(@class, "whitespace-pre-wrap")]'):
            element.set('class', 'changed-prose')
    else:
        for element in root.xpath('//div[contains(@class, "--profile-avatar-size:")]'):
            element.set('class', 'changed-profile')
    changed = html.tostring(root, encoding='unicode')
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    disclosure = coverage()
    assert sites.extract_document(changed, url, coverage=disclosure) == extract(changed, url)
    assert disclosure['site_adapter'] == {'name': 'x', 'outcome': 'fallback'}


@pytest.mark.parametrize('name,code,reason', [
    ('thread-probe', 'fetch_failed', 'content_unavailable'),
    ('login-wall', 'blocked', 'login_required'),
])
def test_real_walls_are_failures(monkeypatch, name, code, reason):
    document, url = fixture(name)
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    with pytest.raises(FetchError) as error:
        sites.extract_document(document, url, coverage=coverage())
    assert error.value.code == code
    assert error.value.details == {'reason': reason}


def test_unavailable_words_in_prose_are_not_classified():
    document = '''<main><h1>Public help</h1><p>Post Not Found:
    private, deleted or only available on the app</p></main>'''
    assert ADAPTER.extract(document, 'https://x.com/help') is None
