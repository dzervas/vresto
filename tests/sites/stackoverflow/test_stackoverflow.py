"""Captured public threads and deliberate Stacks markup drift."""
from dataclasses import replace
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from aiohttp import web
from lxml import html
import pytest
from test_fetch import site
from test_http import body, client, running

from kagi_crawler import sites
from kagi_crawler.extraction import extract
from kagi_crawler.sites.stackoverflow import ADAPTER
from kagi_crawler.types import Coverage

DIRECTORY = Path(__file__).parent


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))


def _fixture(name):
    path = DIRECTORY / name
    return (path.with_suffix('.html').read_text(),
            path.with_suffix('.url').read_text().strip())


@pytest.mark.parametrize('name', ['yield', 'redirect'])
def test_captured_golden(name, registered):
    document, url = _fixture(name)
    coverage: Coverage = {}
    result = sites.extract_document(document, url, coverage=coverage)
    assert result.markdown == (DIRECTORY / name).with_suffix('.md').read_text()
    assert coverage['site_adapter'] == {'name': 'stackoverflow', 'outcome': 'applied'}
    assert '(accepted)' in result.markdown
    assert 'Asked:' in result.markdown and 'Answered:' in result.markdown
    assert 'Edited:' in result.markdown
    assert '### Comment by ' in result.markdown
    assert 'Score:' in result.markdown
    for chrome in ['Hot Network Questions', 'Related questions', 'Ask Question',
                   'Copy link', 'Share a link', 'Up vote']:
        assert chrome not in result.markdown
    assert all(link.startswith(('https://', 'http://')) for link in result.links)
    if name == 'yield':
        assert '```py\ndef _get_child_candidates' in result.markdown
        assert 'This comment was edited 2 times' in result.markdown
        assert result.markdown.count('\n## Answer ') == 30
        assert 'Jochen Schulz' in result.markdown
        assert 'cannot' in result.markdown
        assert 'Copy\n' not in result.markdown
    else:
        assert 'This question already has answers here' in result.markdown
        assert 'Closed 12 years ago' in result.markdown
        assert 'Note, however, that the hosting provider' in result.markdown


@pytest.mark.parametrize('name', ['yield', 'redirect'])
@pytest.mark.parametrize('old,new', [('js-post-body', 'changed-body'),
                                    ('comment-copy', 'changed-comment')])
def test_markup_drift_falls_back_with_disclosure(name, old, new, registered):
    document, url = _fixture(name)
    document = document.replace(old, new)
    coverage: Coverage = {}
    assert sites.extract_document(document, url, coverage=coverage) == extract(document, url)
    assert coverage['site_adapter'] == {'name': 'stackoverflow', 'outcome': 'fallback'}


@pytest.mark.parametrize('url,expected', [
    ('https://stackoverflow.com/questions/231767/title', True),
    ('https://meta.stackoverflow.com/questions/123/title', True),
    ('https://ru.stackoverflow.com/questions/123/title', True),
    ('https://unix.stackexchange.com/questions/99263/title', True),
    ('https://meta.math.stackexchange.com/questions/123/title', True),
    ('https://superuser.com/questions/123/title', True),
    ('https://meta.serverfault.com/questions/123/title', True),
    ('https://askubuntu.com/questions/123/title', True),
    ('https://mathoverflow.net/questions/123/title', True),
    ('https://stackoverflow.com.evil.example/questions/123/title', False),
    ('https://notstackoverflow.com/questions/123/title', False),
    ('https://stackexchange.com/questions/123/title', False),
    ('https://stackapps.com/questions/123/title', False),
    ('https://stackoverflow.com/questions/tagged/python', False),
    ('https://stackoverflow.com/users/123/person', False),
])
def test_host_and_page_match(url, expected):
    assert ADAPTER.matches(urlsplit(url)) is expected


def test_answer_order_is_dom_order_not_accepted_first(registered):
    document, url = _fixture('yield')
    root = html.fromstring(document)
    accepted = root.xpath('//*[@id="answer-231855"]')[0]
    other = root.xpath('//*[@id="answer-237028"]')[0]
    accepted.getparent().remove(accepted)
    other.addnext(accepted)
    result = sites.extract_document(cast(str, html.tostring(root, encoding='unicode')), url)
    assert result.markdown.index('## Answer 237028') < result.markdown.index('## Answer 231855')
    assert '## Answer 231855 by Bite code (accepted)' in result.markdown
    # Original H1s in answers must not become sibling answer boundaries.
    assert '\n## Lazy Iterators' not in result.markdown
    assert '\n### Lazy Iterators' in result.markdown


def test_deleted_author_and_wiki_qualification(registered):
    document, url = _fixture('redirect')
    root = html.fromstring(document)
    answer = root.xpath('//*[@id="answer-99264"]')[0]
    answer.attrib.pop('data-author-username')
    signature = html.fragment_fromstring('''<div class="post-signature">
    <span class="community-wiki" title="This post is community owned as of 2018-01-01">community wiki</span>
    <div class="user-details">2 revs, 2 users</div></div>''')
    answer.append(signature)
    result = sites.extract_document(cast(str, html.tostring(root, encoding='unicode')), url)
    assert 'Answer 99264 by Unknown author (accepted)' in result.markdown
    assert 'community wiki' in result.markdown
    assert '2 revs, 2 users' in result.markdown
    assert 'This post is community owned as of 2018-01-01' in result.markdown


async def test_native_show_more_anchor_loads_comments_but_not_nav_or_form_anchors(tmp_path, monkeypatch):
    # Real captured control shape; no site-specific DOM rewrite to defeat guards.
    monkeypatch.setattr(sites, 'ADAPTERS', (replace(ADAPTER, matches=lambda url: True),))
    document, _ = _fixture('redirect')
    root = html.fromstring(document)
    control = root.xpath('//*[contains(@class,"js-show-link") and @aria-controls]')[0]
    control.set('class', 'js-show-link comments-link')
    control.text = 'Show 1 more comment'
    # Native handlers stand in for Stacks' same-document comment request.
    target = root.xpath('//*[@id=$id]', id=control.get('aria-controls'))[0]
    comment = "<li data-comment-id='local'><span class='comment-user'>Local author</span><span class='comment-copy'>Loaded native comment qualification.</span></li>"
    control.set('onclick', f"document.getElementById('{target.get('id')}').insertAdjacentHTML('beforeend', {comment!r});this.remove();return false")
    # Identical authorized selector/association, but forbidden ancestors.
    for tag in ('nav', 'form'):
        wrapper = html.Element(tag)
        forbidden = html.fromstring(html.tostring(control, encoding='unicode'))
        forbidden.set('onclick', "document.querySelector('.js-post-body').append('UNSAFE_CLICK');return false")
        wrapper.append(forbidden)
        root.xpath('//*[@id="question"]')[0].append(wrapper)
    markup = cast(str, html.tostring(root, encoding='unicode'))

    async def handler(request):
        return web.Response(content_type='text/html', text=markup)

    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'error' not in source, source
        assert 'UNSAFE_CLICK' not in source['content']
        assert 'Loaded native comment qualification.' in source['content']
        assert 'Comment by Local author' in source['content']
        assert source['coverage']['actions'] >= 1
        assert source['coverage']['site_adapter'] == {'name': 'stackoverflow', 'outcome': 'applied'}
