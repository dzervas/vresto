"""Golden canonical Markdown for real rendered pages (tests/fixtures/extraction).

Each NAME.html (+ NAME.url) is a rendered snapshot captured with
scripts/capture_fixture.py (headless Chromium, 2026-10-14; StackOverflow
and Reddit served challenge/login pages, so site-adapter tickets capture them
separately); NAME.md is the reviewed extraction output. Any
extraction change shows up here as a Markdown diff. Regenerate with
UPDATE_GOLDEN=1 and review the diff by hand before accepting it.
"""
import os
from pathlib import Path

import pytest

from kagi_crawler.extraction import extract

FIXTURES = Path(__file__).parent / 'fixtures' / 'extraction'
NAMES = sorted(p.stem for p in FIXTURES.glob('*.html'))


@pytest.mark.parametrize('name', NAMES)
def test_golden_markdown(name):
    document = (FIXTURES / f'{name}.html').read_text()
    url = (FIXTURES / f'{name}.url').read_text().strip()
    golden = FIXTURES / f'{name}.md'
    markdown = extract(document, url).markdown
    if os.environ.get('UPDATE_GOLDEN') == '1':
        golden.write_text(markdown)
    assert markdown == golden.read_text()
