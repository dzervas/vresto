"""Targeted page types: public Reddit post/comment threads (currently declined).
Acquisition/markup assumptions (verified 2026-10-14, logged-out headed Chrome):
www.reddit.com returns "Prove your humanity"; old.reddit.com redirects to
/login/?reason=lor2 and explicitly requires an account; the www .json view
returns "You've been blocked by network security", not JSON. Walls use h1
headings or a font-bold text-24 security title. These results
also held when navigating all three views in the same fresh browser context.
No credentials, supplied cookies, or CAPTCHA solving were used.

There is no acquirable thread fixture, so no stable representation can be
selected and no post/comment extraction or expansion is implemented. The three
observed walls report blocked/login_required, never successful thread content.
Unrecognized markup still declines to disclosed generic fallback.

Known fragility: the old login page itself contains shreddit/faceplate web
components with open shadow roots, but no shreddit-post/shreddit-comment.
page.content() captures light DOM only. Shadow-root serialization is a browser
concern; it cannot be repaired here or validated against a thread we never
received. Old-Reddit URL rewriting is not a solution to the observed login wall.

Fixtures: tests/sites/reddit/{www,old,json}.{html,url,md}; these are real wall
snapshots, NOT post/comment goldens. All use the public Obama AMA at
/r/IAmA/comments/z1c9z/i_am_barack_obama_president_of_the_united_states_ama/.
Refresh (use www/old hosts and .json suffix; headed setup in AGENTS.md):
uv run --locked python scripts/capture_fixture.py URL tests/sites/reddit/NAME --headed --channel chrome --settle 8
Review wall classification after capture. Implement specialized
extraction only after a real logged-out thread with nested comments is captured.
"""
from urllib.parse import SplitResult

from lxml import html
from .base import ExtractionOutcome, SiteAdapter


def _matches(url: SplitResult) -> bool:
    # Match only the evaluated public hosts, including redirected login URLs.
    return url.hostname in {'reddit.com', 'www.reddit.com', 'old.reddit.com'}


def _extract(document: str, _url: str) -> ExtractionOutcome | None:
    # Wall headings survive the observed light/shadow DOM wrappers. Restrict
    # to headings/the captured security title, never phrase matches in prose.
    root = html.document_fromstring(document)
    for node in root.xpath('//script | //style'):
        node.drop_tree()
    headings = root.xpath('//h1 | //div[contains(concat(" ", normalize-space(@class), " "), " text-24 ") and contains(concat(" ", normalize-space(@class), " "), " font-bold ")]')
    if any(' '.join(node.text_content().split()).rstrip('.') in (
            'Prove your humanity', 'Log in to use old Reddit',
            "You've been blocked by network security") for node in headings):
        return ExtractionOutcome('blocked')
    # No observed post markup: deliberately decline rather than invent selectors.
    return None


ADAPTER = SiteAdapter(name='reddit', matches=_matches, extract=_extract)
