"""GitHub expansion through the HTTP boundary; localhost is test-only matching."""
from dataclasses import replace
import os
from pathlib import Path

from lxml import html

import pytest
from aiohttp import web
from test_fetch import site
from test_http import running, client, body

from vresto import sites
from vresto.evidence import sections
from vresto.extraction import extract
from vresto.sites.github import ADAPTER


@pytest.fixture(autouse=True)
def local_github(monkeypatch):
    # Serve GitHub markup locally without changing production hostname matching.
    monkeypatch.setattr(sites, 'ADAPTERS', (replace(ADAPTER, matches=lambda url: True, extract=None),))


async def test_github_class_scope_and_identical_comment_repeats(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<h1>Discussion</h1>
        <div class="discussion-timeline"><div id="comments">
        <div class="timeline-comment" id="one"><h2>Alice</h2><p>Initial discussion.</p></div></div>
        <button type="button" aria-controls="comments" onclick="load()">Load more comments</button></div>
        <script>function load() { document.querySelector('#comments').insertAdjacentHTML('beforeend',
        '<div class="timeline-comment" id="one"><h2>Alice</h2><p>Initial discussion.</p></div>' +
        '<div class="timeline-comment" id="one"><p>Warning: a different qualification survives.</p></div>');
        document.querySelector('button').remove(); }</script>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'error' not in source, source
        assert source['content'].count('Initial discussion.') == 1
        assert 'Warning: a different qualification survives.' in source['content']
        assert source['coverage']['actions'] == 1
        assert source['coverage']['site_adapter'] == {'name': 'github', 'outcome': 'applied'}


def test_discussion_fixture_matches_generic_golden():
    directory = Path(__file__).parent
    result = extract((directory / 'discussion.html').read_text(), 'https://github.com/example/repo/issues/1')
    assert result.markdown == (directory / 'discussion.md').read_text()


@pytest.mark.parametrize('expansion', [
    sites.Expansion(scope_selector='['),
    sites.Expansion(authorize_selector='['),
    sites.Expansion(deduplicate='() => { throw new Error("private fixture URL"); }'),
])
async def test_browser_phase_markup_failure_uses_generic_without_restarting_actions(
        tmp_path, monkeypatch, expansion, caplog):
    monkeypatch.setattr(sites, 'ADAPTERS', (replace(ADAPTER, matches=lambda url: True,
        expansion=lambda: expansion),))
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Public discussion</h1>
        <section id="comments"><p>Warning: original qualification.</p></section>
        <button type="button" aria-controls="comments"
        onclick="document.querySelector('#comments').append(' Additional qualification.');this.remove()">Load more comments</button></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'error' not in source, source
        assert 'Additional qualification.' in source['content']
        assert source['coverage']['actions'] == 1
        assert source['coverage']['site_adapter'] == {'name': 'github', 'outcome': 'fallback'}
        assert 'private fixture URL' not in caplog.text


@pytest.mark.parametrize('name', ['issue', 'review', 'thread', 'blob', 'cancellation'])
def test_captured_golden(name, monkeypatch):
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    path = Path(__file__).parent / name
    document = path.with_suffix('.html').read_text()
    url = path.with_suffix('.url').read_text().strip()
    coverage = {}
    result = sites.extract_document(document, url, coverage=coverage)
    assert coverage['site_adapter'] == {'name': 'github', 'outcome': 'applied'}
    if os.environ.get('UPDATE_GOLDEN') == '1':
        path.with_suffix('.md').write_text(result.markdown)
    assert result.markdown == path.with_suffix('.md').read_text()
    assert 'Navigation Menu' not in result.markdown
    assert 'Copy Markdown' not in result.markdown
    assert all(link.startswith('https://') for link in result.links)
    if name == 'issue':
        assert 'State: Closed' in result.markdown
        assert '```python' in result.markdown
        assert 'Yes this should be addressed in 2.32.5' in result.markdown
        assert '2024-05-31T10:26:46.000Z' in result.markdown
        assert '#### Reproduction Steps' in result.markdown
        assert result.markdown.count('This issue appears to be leading') == 1
    elif name == 'review':
        assert 'Review comment' in result.markdown
        assert '```diff' in result.markdown
        assert 'custome Context' in result.markdown
        assert 'Resolving in favor of' in result.markdown
    elif name == 'thread':
        assert 'lovelydinosaur' in result.markdown
        assert 'Maintainer' in result.markdown
        assert 'Unanswered' in result.markdown
        assert 'NUM_THREADS' in result.markdown
        assert 'Comment 10 (reply to comment 9)' in result.markdown
        assert 'Author; Maintainer' not in result.markdown
    elif name == 'blob':
        assert result.markdown.startswith('# Requests')
    else:
        assert '```python' in result.markdown
        assert '```diff' in result.markdown
        assert 'Comment 70' in result.markdown


@pytest.mark.parametrize('name', ['issue', 'review', 'thread', 'blob'])
def test_captured_markup_drift_falls_back(name, monkeypatch):
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    path = Path(__file__).parent / name
    root = html.fromstring(path.with_suffix('.html').read_text())
    # Simulate renderer renaming its body hook, leaving prose intact.
    for element in root.xpath('//*[contains(@class,"markdown-body")]'):
        element.set('class', element.get('class', '').replace('markdown-body', 'changed-body'))
        element.attrib.pop('data-testid', None)
    document = html.tostring(root, encoding='unicode')
    url = path.with_suffix('.url').read_text().strip()
    coverage = {}
    result = sites.extract_document(document, url, coverage=coverage)
    assert result == extract(document, url)
    assert coverage['site_adapter'] == {'name': 'github', 'outcome': 'fallback'}


@pytest.mark.parametrize('body_class, body_hook', [
    ('markdown-body', 'data-testid="markdown-body"'),
    ('comment-body markdown-body', ''),
])
@pytest.mark.parametrize('code, language', [
    ('<div class="highlight highlight-source-python"><pre><span># code comment</span>\n'
     '    pass</pre></div>', 'python'),
    ('<div class="snippet-clipboard-content"><pre><code># code comment\n'
     '    pass</code></pre></div>', ''),
    ('<table class="highlight highlight-source-python">'
     '<tr><td class="blob-num">1</td><td class="blob-code"># code comment</td></tr>'
     '<tr><td class="blob-num">2</td><td class="blob-code">    pass</td></tr>'
     '</table>', 'python'),
])
def test_react_and_legacy_code_blocks(body_class, body_hook, code, language):
    document = f'''<h1>Issue</h1><div class="timeline-comment">
    <a data-hovercard-type="user">author</a><relative-time datetime="2025-01-01"></relative-time>
    <div class="{body_class}" {body_hook}>{code}
    <table><tr><th>Data</th></tr><tr><td>value</td></tr></table></div></div>'''
    assert ADAPTER.extract is not None
    result = ADAPTER.extract(document, 'https://github.com/example/repo/issues/1')
    assert result is not None
    assert f'```{language}\n# code comment\n    pass\n```' in result.markdown
    assert '| Data |' in result.markdown


def test_cancellation_comment_code_cannot_become_headings(monkeypatch):
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    path = Path(__file__).parent / 'cancellation'
    result = sites.extract_document(path.with_suffix('.html').read_text(),
                                    path.with_suffix('.url').read_text().strip())
    # These lines appear in a source-permalink table, a plain pre/code block,
    # and a syntax-highlighted Python pre. All three must stay inside fences.
    markers = ('# If there are no pending cancellations left,',
               "# don't propagate CancelledError.")
    fenced = False
    counts = dict.fromkeys(markers, 0)
    for line in result.markdown.splitlines():
        if line.startswith('```'):
            fenced = not fenced
        for marker in markers:
            if marker in line:
                assert fenced, line
                counts[marker] += 1
    assert not fenced
    assert set(counts.values()) == {3}
    assert not any(title and any(marker[2:] in title for marker in markers)
                   for _, _, title in sections(result.markdown))


def test_missing_comment_author_does_not_borrow_from_timeline(monkeypatch):
    monkeypatch.setattr(sites, 'ADAPTERS', (ADAPTER,))
    path = Path(__file__).parent / 'review'
    root = html.fromstring(path.with_suffix('.html').read_text())
    for author in root.xpath('//*[@id="discussion_r1622885176"]//a[@data-hovercard-type="user"]'):
        author.drop_tree()
    coverage = {}
    document = html.tostring(root, encoding='unicode')
    url = path.with_suffix('.url').read_text().strip()
    assert sites.extract_document(document, url, coverage=coverage) == extract(document, url)
    assert coverage['site_adapter']['outcome'] == 'fallback'
