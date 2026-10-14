"""Capture a rendered page as an extraction fixture: HTML in, golden Markdown out.

    uv run python scripts/capture_fixture.py URL tests/fixtures/extraction/NAME
    [--headed] [--channel chrome|chromium] [--executable PATH] [--settle SECONDS]

Challenge-protected sites (StackOverflow, Reddit, X) usually need --headed
(requires a DISPLAY, e.g. a private Xvfb) and a real Chrome; --settle waits
for native challenge continuation before reading the DOM.

Writes NAME.html (rendered DOM after native settling, script/style bodies
stripped to keep fixtures small) and NAME.url. Then regenerate goldens with
    UPDATE_GOLDEN=1 uv run pytest tests/test_extraction_golden.py
and review the Markdown diff by hand: goldens are only as good as that review.

Fixtures are third-party page snapshots for local regression tests only; they
are excluded from the sdist (pyproject allowlist includes tests/*.py only).
"""
import argparse
import asyncio
from pathlib import Path

from lxml import html
from patchright.async_api import async_playwright


async def capture(url, target, *, headed=False, channel=None, executable=None, settle=0.0):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed, channel=channel,
                                                   executable_path=executable)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=30_000)
            try:
                await page.wait_for_load_state('networkidle', timeout=5_000)
            except Exception:
                pass  # Persistent connections; the DOM is still usable.
            if settle:
                await asyncio.sleep(settle)
            document, final_url = await page.content(), page.url
        finally:
            await browser.close()
    root = html.document_fromstring(document)
    for element in root.xpath('//script | //style | //noscript | //template | //svg'):
        element.text = None
        for child in list(element):
            element.remove(child)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_suffix('.html').write_text(html.tostring(root, encoding='unicode', doctype='<!DOCTYPE html>'))
    target.with_suffix('.url').write_text(final_url + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('url')
    parser.add_argument('target', type=Path, help='fixture path without suffix')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--channel', choices=['chrome', 'chromium'])
    parser.add_argument('--executable', help='explicit browser binary (e.g. a local Chrome)')
    parser.add_argument('--settle', type=float, default=0.0, help='extra seconds before reading the DOM')
    args = parser.parse_args()
    asyncio.run(capture(args.url, args.target, headed=args.headed, channel=args.channel,
                        executable=args.executable, settle=args.settle))


if __name__ == '__main__':
    main()
