"""Targeted page types: public X/Twitter posts, visible threads/reply previews,
and profile summaries with their initially rendered posts (no pagination).
Markup assumptions (verified 2026-10-14, fresh logged-out headed Chrome):
<main> contains <article> posts, profile links label author/handle, status links
label dates, dir=auto whitespace-pre-wrap divs contain prose. Engagement counts
have aria-labels. No data-testid=tweet exists in these captures.
Logged-out findings: jack/status/20 worked on both hosts with three replies;
BarackObama/status/896523232098078720 and elonmusk/status/1585841080431321088
worked with continuation/reply previews. naval/status/1002103360646823936
rendered 43 articles (including a separately attributed nested quote). NASA
profiles worked on both hosts with five posts. NASA/following redirected to
/i/jf/onboarding/web?mode=login and exposed only an authentication form.
twitter.com redirected to x.com. See-all-replies/full-profile controls lead to
login prompts: these are previews, NOT exhaustive threads/replies/timelines.
patio11/status/1429559287234785280 returned Post Not Found with an explicit
private/deleted/app-only warning; this does not prove a universal login wall.
Known fragility: X changes markup/localization and logged-out availability;
media is linked, not transcribed. Dates are preserved verbatim (including
relative dates); no missing year is invented. No API/auth/cookies/CAPTCHA use.
Login forms and explicit unavailable-post warnings are failures, not content.
Public previews disclose partial coverage; the Markdown qualification is retained
so copied content still carries its logged-out limitation.
Fixtures: tests/sites/x/*.html, *.url, *.md (real public captures, 2026-10-14).
Refresh (replace URL and NAME; headed setup in AGENTS.md):
uv run --locked python scripts/capture_fixture.py URL tests/sites/x/NAME --headed --channel chrome --settle 8
Then review/update preview Markdown through sites.extract_document; wall snapshots
are failure regressions, not successful content goldens. Never log in or solve a challenge to refresh a fixture.
"""
from copy import deepcopy
import re
from urllib.parse import urlsplit
from typing import cast

import lxml.etree as etree
from lxml import html

from ..extraction import Extracted, extract
from .base import ExtractionOutcome, SiteAdapter

_STATUS = re.compile(r'^/[^/]+/status/\d+$')


def _extract(document: str, url: str) -> Extracted | ExtractionOutcome | None:
    root = html.document_fromstring(document)
    # Public preview posts are semantic articles under main, unlike sidebar people.
    articles = root.xpath('//main//article')
    if not articles:
        for node in root.xpath('//script | //style'):
            node.drop_tree()
        text = ' '.join(root.text_content().split())
        if any('Email or username' in form.text_content() for form in root.xpath('//form')):
            return ExtractionOutcome('blocked')
        if (root.xpath('//title[normalize-space(.)="Post Not Found - X | 404 Error"]')
                and 'private, deleted or only available on the app' in text):
            return ExtractionOutcome('unavailable')
        return None
    output = html.Element('main')
    etree.SubElement(output, 'h1').text = 'X public preview'
    etree.SubElement(output, 'p').text = (
        'Logged-out preview only: additional replies, posts, and full profile '
        'access may require login. This is not an exhaustive conversation or timeline.')
    if '/status/' not in urlsplit(url).path:
        # The profile identity/bio/follower block has this specific sizing variable.
        profiles = root.xpath('//main//div[contains(@class, "--profile-avatar-size:")]')
        if len(profiles) != 1:
            return None
        profile = deepcopy(profiles[0])
        # Avatar/banner artwork and follow/menu controls are profile chrome.
        for node in profile.xpath('.//img | .//svg | .//button | .//*[@role="button"]'):
            if node.getparent() is not None:
                node.getparent().remove(node)
        output.append(profile)
    for original in articles:
        # Quoted posts are nested articles. Extract them separately with their
        # own attribution, never mix their date/prose into the quoting author.
        quoted = bool(original.xpath('ancestor::article'))
        article = deepcopy(original)
        for nested in article.xpath('.//article'):
            nested.getparent().remove(nested)
        # Date links have bare status paths; media/photo links have extra segments.
        dates = [a for a in article.xpath('.//a[@href]')
                 if _STATUS.fullmatch(a.get('href', '')) and a.text_content().strip()]
        # The handle is a profile link whose visible text starts with @.
        handles = [a for a in article.xpath('.//a[@href]')
                   if a.text_content().strip().startswith('@')
                   and a.get('href') == '/' + a.text_content().strip()[1:]]
        if len(dates) != 1 or not handles:
            return None
        handle = handles[0].text_content().strip()
        authors = [a.text_content().strip() for a in article.xpath('.//a[@href]')
                   if a.get('href') == handles[0].get('href')
                   and a.text_content().strip() and not a.text_content().strip().startswith('@')]
        if not authors:
            return None
        section = etree.SubElement(output, 'section')
        prefix = 'Quoted post: ' if quoted else ''
        etree.SubElement(section, 'h2').text = f'{prefix}{authors[0]} ({handle}) — {dates[0].text_content().strip()}'
        permalink = etree.SubElement(etree.SubElement(section, 'p'), 'a', href=dates[0].get('href'))
        permalink.text = 'Post permalink'
        # X's prose and link-card summaries use whitespace-pre-wrap. Keep the
        # inline markup/links/newlines, not just text (qualifications matter).
        bodies = article.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " whitespace-pre-wrap ")]')
        if not bodies:
            return None  # Even media-only previews have an empty prose div.
        for body in bodies:
            if not any(parent in bodies for parent in body.iterancestors()):
                section.append(deepcopy(body))
        # External link cards wrap a div summary in a target=_blank anchor;
        # preserve their source/title/description, without duplicate inline links.
        for card in article.xpath('.//a[@target="_blank"][div]'):
            if not any(parent in bodies for parent in card.iterancestors()):
                section.append(deepcopy(card))
        # Media is sometimes CSS-backed, so retain its accessible link rather
        # than pretending the image/video content was extracted or transcribed.
        for media in article.xpath('.//a[@aria-label="View media" or starts-with(@aria-label, "Open broadcast:")]'):
            link = etree.SubElement(etree.SubElement(section, 'p'), 'a', href=media.get('href'))
            link.text = media.get('aria-label')
        # Aria-labelled engagement controls carry visible compact counts. Convert
        # those counts to labelled prose before dropping all interactive chrome.
        counts = []
        for label in ('Reply', 'Repost', 'Like', 'Bookmark', 'View count'):
            controls = article.xpath('.//*[@aria-label=$label]', label=label)
            if controls and controls[0].text_content().strip():
                counts.append(f'{label}: {controls[0].text_content().strip()}')
        if counts:
            etree.SubElement(section, 'p').text = '; '.join(counts)
    return ExtractionOutcome('partial', extract(cast(str, html.tostring(output, encoding='unicode')), url))


# Exact known public hosts only; never match lookalikes or unrelated subdomains.
ADAPTER = SiteAdapter(name='x', matches=lambda url: url.hostname in
                      ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'), extract=_extract)
