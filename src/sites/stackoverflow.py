"""Targeted page types: public Stack Overflow/Stack Exchange question threads.
Markup assumptions (verified 2026-10-14): #question-header h1, #question,
#answers .answer with data-answerid/data-score, .js-post-body, .post-signature,
and li[data-comment-id] containing .comment-copy. Accepted uses accepted-answer.
Known fragility: Stacks classes/attributes can change; decline the entire thread
if a post body/score disappears. Preserve DOM answer order, not score order.
Only the acquired page is covered (no answer pagination). Native show-more
comment anchors are authorized by their verified Stacks classes/aria-controls;
the generic loop enforces all read-only guards and bounds.
Hosts: stackoverflow.com and its subdomains (including localized/meta sites),
*.stackexchange.com, and the standalone network sites superuser.com,
serverfault.com, askubuntu.com, mathoverflow.net (including meta subdomains).
Not arbitrary lookalikes, stackexchange.com directory pages, or Stack Apps.
Fixtures: tests/sites/stackoverflow/{yield,redirect}.{html,url,md}.
Refresh (public pages only; headed setup in AGENTS.md):
uv run --locked python scripts/capture_fixture.py URL tests/sites/stackoverflow/NAME --headed --channel chrome --settle 8
Then extract through ADAPTER.extract and review the golden Markdown.
"""
from copy import deepcopy
import re
from typing import cast
from urllib.parse import SplitResult

from lxml import html

from ..extraction import Extracted, extract
from .base import Expansion, SiteAdapter


# Exact DNS boundaries admit localized/meta network hosts but not suffix spoofs.
_HOSTS = ('stackoverflow.com', 'superuser.com', 'serverfault.com',
          'askubuntu.com', 'mathoverflow.net')


def _matches(url: SplitResult) -> bool:
    host = url.hostname or ''
    return (url.scheme in {'http', 'https'}
            and bool(re.match(r'^/questions/\d+(?:/|$)', url.path))
            and (any(host == base or host.endswith('.' + base) for base in _HOSTS)
                 or host.endswith('.stackexchange.com')))


def _class(name: str) -> str:
    # Stacks uses multiple classes per node; substring matches would admit ads.
    return f'contains(concat(" ", normalize-space(@class), " "), " {name} ")'


def _text(element) -> str:
    return ' '.join(element.text_content().split())


def _append(parent, tag: str, text: str):
    element = html.Element(tag)
    element.text = text
    parent.append(element)
    return element


def _body(source):
    body = deepcopy(source)
    # Stacks injects a copy-button toolbar into <pre>; keep code only, including
    # highlighted spans and explicit lang-/language- classes for the converter.
    for toolbar in body.xpath('.//pre/div[@data-nosnippet]'):
        toolbar.drop_tree()
    # Post-local headings must stay below the question/answer heading.
    for heading in body.xpath('.//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6'):
        heading.tag = f'h{min(max(int(heading.tag[1]) + 1, 3), 6)}'
    return body


def _metadata(post, target) -> None:
    # data-author-username is the original poster, not the latest editor.
    author = post.get('data-author-username')
    if author:
        _append(target, 'p', f'Author: {author}')
    # Signature action-time spans carry absolute UTC titles, even when their
    # visible labels are relative. Wiki signatures can lack a posting date.
    for signature in post.xpath(f'.//*[{_class("post-signature")}]'):
        actions = signature.xpath(f'.//*[{_class("user-action-time")}]')
        for action in actions:
            dates = action.xpath('.//span[@title]/@title')
            if not dates:
                continue
            label = _text(action).split()[0].capitalize()
            users = signature.xpath(f'.//*[{_class("user-details")}]//a[starts-with(@href,"/users/")]')
            by = f' by {_text(users[0])}' if users else ''
            _append(target, 'p', f'{label}: {dates[0]}{by}')
        # Community ownership and revision/contributor attribution qualify the
        # original-author metadata; don't silently present wiki posts as solo work.
        wiki = signature.xpath(f'.//*[{_class("community-wiki")}]')
        if wiki:
            _append(target, 'p', _text(signature))
            if wiki[0].get('title'):
                _append(target, 'p', wiki[0].get('title'))


def _comments(post, target) -> None:
    # Identified comment <li>s exclude interleaved native-comment-ad list items.
    for comment in post.xpath('.//li[@data-comment-id]'):
        copies = comment.xpath(f'.//*[{_class("comment-copy")}]')
        if len(copies) != 1:
            raise ValueError('comment markup drift')
        # comment-user may be an unlinked span for a deleted account.
        users = comment.xpath(f'.//*[{_class("comment-user")}]')
        author = _text(users[0]) if users else 'Unknown author'
        heading = f'Comment by {author}'
        # Hidden schema.org datePublished contains an absolute posting date.
        dates = comment.xpath('.//*[@itemprop="datePublished"]')
        if dates:
            heading += f' — {_text(dates[0])}'
        _append(target, 'h3', heading)
        score = comment.get('data-comment-score')
        if score is not None:
            _append(target, 'p', f'Score: {score}')
        target.append(deepcopy(copies[0]))
        # The pencil tooltip records edit counts, not an edit timestamp.
        for note in comment.xpath('.//*[@title]'):
            title = note.get('title', '')
            if title.startswith('this comment was edited '):
                _append(target, 'p', title.capitalize())


def _extract(document: str, url: str) -> Extracted | None:
    root = html.document_fromstring(document)
    # These IDs distinguish question detail pages from listings/challenge pages.
    titles = root.xpath('//*[@id="question-header"]//h1')
    questions = root.xpath('//*[@id="question"]')
    if len(titles) != 1 or len(questions) != 1:
        return None
    # DOM order is the viewer's current answer sort; never move accepted first.
    posts = questions + root.xpath(f'//*[@id="answers"]//*[{_class("answer")}]')
    main = html.Element('main')
    _append(main, 'h1', _text(titles[0]))
    for index, post in enumerate(posts):
        bodies = post.xpath(f'.//*[{_class("js-post-body")}]')
        score = post.get('data-score')
        if len(bodies) != 1 or score is None or not re.fullmatch(r'-?\d+', score):
            return None
        if index and not post.get('data-answerid'):
            return None
        author = post.get('data-author-username') or 'Unknown author'
        heading = 'Question' if index == 0 else f'Answer {post.get("data-answerid")} by {author}'
        # accepted-answer is on the answer container, not a vote button tooltip.
        if 'accepted-answer' in post.get('class', '').split():
            heading += ' (accepted)'
        article = html.Element('article')
        main.append(article)
        _append(article, 'h2', heading)
        _append(article, 'p', f'Score: {score}')
        _metadata(post, article)
        article.append(_body(bodies[0]))
        _comments(post, article)
    return extract(cast(str, html.tostring(main, encoding='unicode')), url)


def _expansion() -> Expansion:
    # Only structurally associated Stacks comment controls, not arbitrary links.
    return Expansion(scope_selector='#question, #answers',
                     authorize_selector='a.js-show-link.comments-link[aria-controls]')


ADAPTER = SiteAdapter(name='stackoverflow', matches=_matches,
                      expansion=_expansion, extract=_extract)
