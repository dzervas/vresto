"""Targeted page types: public issues, PR conversations/reviews, discussions,
and rendered Markdown blob/README views.
Markup assumptions (verified 2026-10-14): React issues use data-testid markdown
bodies; legacy conversations use markdown-body plus user hovercards and
relative-time. Inline reviews use review-thread-collapsible and blob-code cells.
Rendered README blobs expose article.markdown-body. Generic extraction retained
GitHub navigation/file-tree chrome in the captured README, so article scoping is
needed; no alternate raw URL acquisition is needed for that view.
Known fragility: React/legacy layouts coexist; renamed body/attribution markup
falls back. Only loaded comments/diffs are available; bounded native expansion
is unchanged. Non-Markdown source blobs and PR files/commits tabs use generic
fallback, not inferred code languages or API/raw-content requests.
Fixtures: tests/sites/github/{issue,review,thread,blob}.{html,url,md}, captured
2026-10-14 logged out/headless (anonymous hidden form/transport tokens and
short-lived signed image queries stripped).
discussion.html/md and browser tests are synthetic expansion regressions.
Refresh (URLs in adjacent .url files):
uv run --locked python scripts/capture_fixture.py URL tests/sites/github/NAME --settle 3
Regenerate: UPDATE_GOLDEN=1 uv run --locked --no-sync pytest tests/sites/github/test_github.py -k captured
Review Markdown before accepting.
"""
from copy import deepcopy
import re
from typing import cast
from urllib.parse import urlsplit

from lxml import html

from ..extraction import Extracted, extract
from .base import Expansion, SiteAdapter

# Suppress only identical repeated *identified* discussion items. Different
# qualifications, authors, code or warnings under the same ID are never removed.
_DEDUP = '''() => {
    const seen = new Map();
    for (const el of document.querySelectorAll('.timeline-comment[id]')) {
        const id = el.id;
        const value = el.innerHTML;
        if (seen.get(id)?.has(value)) el.remove();
        else { if (!seen.has(id)) seen.set(id, new Set()); seen.get(id).add(value); }
    }
}'''



def _expansion() -> Expansion:
    return Expansion(scope_selector='main, article, [role="main"], .discussion-timeline',
                     deduplicate=_DEDUP)


def _class(name: str) -> str:
    return f'contains(concat(" ", normalize-space(@class), " "), " {name} ")'


def _child(parent, tag: str, attributes=None):
    child = html.Element(tag, attributes or {})
    parent.append(child)
    return child


def _text(element) -> str:
    return ' '.join(element.text_content().split())


def _body_copy(body, *, conversation=False):
    result = deepcopy(body)
    result.tag = 'div'  # Legacy bodies are presentation-table cells, not data tables.
    if conversation:
        # User Markdown headings nest below the attributed comment heading.
        for heading in result.xpath('.//h1 | .//h2 | .//h3 | .//h4 | .//h5'):
            heading.tag = 'h' + str(min(6, int(heading.tag[1]) + 2))
    # GitHub's copy widgets are controls inside rendered Markdown, not prose.
    for control in result.xpath('.//clipboard-copy | .//button | .//svg'):
        control.drop_tree()
    # GitHub uses highlight-source-X wrappers instead of language-X on pre/code.
    for pre in result.xpath('.//pre'):
        for parent in pre.iterancestors():
            match = re.search(r'(?:^|\s)highlight-source-([\w+#.-]+)', parent.get('class', ''))
            if match:
                pre.set('class', 'language-' + match[1])
                break
    return result


def _extract(document: str, url: str) -> Extracted | None:
    path = urlsplit(url).path
    root = html.document_fromstring(document)
    output = html.Element('article')
    if '/blob/' in path or len(path.strip('/').split('/')) == 2:
        # Only rendered Markdown articles are known; source-code blob layouts differ.
        articles = root.xpath('//article[' + _class('markdown-body') + ']')
        if len(articles) != 1:
            return None
        output.append(_body_copy(articles[0]))
        return extract(cast(str, html.tostring(output, encoding='unicode')), url)
    if not re.fullmatch(r'/[^/]+/[^/]+/(?:issues|pull|discussions)/\d+/?', path):
        return None
    # The first visible h1 is GitHub's page title, not hidden commit dialogs.
    titles = root.xpath('//h1[not(@data-hidden="true") and not(' + _class('sr-only') + ')]')
    if not titles:
        return None
    _child(output, 'h1').text = _text(titles[0])
    # StateLabel is the React issue/PR state; legacy discussions use State.
    states = root.xpath('//*[@data-component="StateLabel"] | //span[' + _class('State') + ']')
    if states:
        _child(output, 'p').text = 'State: ' + _text(states[0])
    # React bodies nest a second markdown-body: select the outer one only.
    bodies = root.xpath('//*[' + _class('markdown-body') + '][not(ancestor::*['
                        + _class('markdown-body') + '])]')
    if not bodies:
        return None
    count = 0
    emitted = {}
    for body in bodies:
        # React issue timeline references also have markdown-body; they are not
        # comments. Legacy PR/discussion bodies carry comment-body instead.
        if body.get('data-testid') != 'markdown-body' and 'comment-body' not in body.get('class', '').split():
            continue
        container = None
        for parent in body.iterancestors():
            # Stop at the nearest comment header, never borrow another author's
            # metadata from the surrounding timeline on markup drift.
            if (parent.get('data-testid', '').startswith('comment-viewer-outer-box-')
                    or 'timeline-comment' in parent.get('class', '').split()
                    or 'review-comment' in parent.get('class', '').split()
                    or parent.get('id', '').startswith('pullrequestreview-')
                    or parent.get('data-testid') == 'issue-body'):
                container = parent
                break
        if container is None:
            return None
        # Review summaries repeat the same ID on an inner body wrapper;
        # their timestamp belongs to the outer wrapper of that same review.
        if container.get('id', '').startswith('pullrequestreview-'):
            for parent in container.iterancestors():
                if parent.get('id') == container.get('id'):
                    container = parent
                    break
        authors = container.xpath('.//a[@data-hovercard-type="user"]')
        dates = container.xpath('.//relative-time[@datetime and string-length(@datetime)>0]')
        authors = [author for author in authors if _text(author)]
        if not authors or not dates:
            return None
        count += 1
        label = 'Description' if count == 1 else 'Comment'
        # review-comment is the legacy inline-review container, with code context
        # in its enclosing thread (not in the comment Markdown itself).
        if 'review-comment' in container.get('class', '').split():
            label = 'Review comment'
        heading = f'{label} {count}'
        # Discussion replies nest under their parent comment container.
        for parent in container.iterancestors():
            if parent in emitted:
                heading += f' (reply to comment {emitted[parent]})'
                break
        emitted[container] = count
        _child(output, 'h2').text = f'{heading} — {_text(authors[0])} — {dates[0].get("datetime")}'
        # Author association labels qualify authority; reactions/menus do not.
        badges = container.xpath('.//*[@data-testid="comment-author-association"] | .//span[' + _class('Label') + ']')
        # Nested replies' badges qualify their own authors, not this comment.
        badges = [b for b in badges if not any(
            'timeline-comment' in p.get('class', '').split()
            for p in b.iterancestors() if p is not container and container in p.iterancestors())]
        if badges:
            _child(output, 'p').text = '; '.join(dict.fromkeys(_text(b) for b in badges if _text(b)))
        if label == 'Review comment':
            threads = list(container.iterancestors('review-thread-collapsible'))
            if threads:
                # The thread file link identifies the code being reviewed.
                files = threads[0].xpath('.//a[contains(@href,"/files/") and contains(@href,"#diff-")]')
                if files:
                    _child(output, 'p').text = 'File: ' + _text(files[0])
                # These cells preserve the loaded diff, including +/- signs.
                cells = threads[0].xpath('.//td[' + _class('blob-code') + ']')
                if cells:
                    pre = _child(output, 'pre', {'class': 'language-diff'})
                    pre.text = '\n'.join(('+' if 'blob-code-addition' in c.get('class', '') else
                                          '-' if 'blob-code-deletion' in c.get('class', '') else ' ')
                                         + ''.join(c.xpath('.//span[' + _class('blob-code-inner') + ']')[0].itertext())
                                         for c in cells if c.xpath('.//span[' + _class('blob-code-inner') + ']'))
        output.append(_body_copy(body, conversation=True))
    if not count:
        return None
    return extract(cast(str, html.tostring(output, encoding='unicode')), url)


ADAPTER = SiteAdapter(name='github', matches=lambda url: url.hostname == 'github.com',
                      expansion=_expansion, extract=_extract)
