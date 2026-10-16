"""Rendered HTML -> canonical Markdown and link targets. Pure: no browser, no I/O.

The only HTML-to-Markdown path. Browser acquisition calls it after DOM
serialization; tests call it directly with fixture HTML (golden outputs in
tests/fixtures/extraction). Changing its output changes canonical text and
therefore new content versions; retained cache versions (and their evidence
IDs) are stored verbatim and never re-extracted.

Pipeline: semantic cleanup (normalization.main_content_html) -> drop
non-content elements -> absolute URLs -> markdownify. Converter options favour
verbatim technical text: no Markdown escaping of identifiers such as
__init__ or *args, underscore emphasis (so a literal `*` parameter stays
distinguishable), ATX headings and fenced code that evidence.py splits on.
"""
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.parse import urljoin

from lxml import html
from markdownify import ATX, UNDERSCORE, MarkdownConverter

from .normalization import main_content_html

LINK_LIMIT = 256

# Never visible document text. iframe/object/embed are separate documents the
# browser does not serialize into this one; their fallback markup is noise.
_NON_CONTENT = ('//head | //script | //style | //link | //meta | //noscript | //template'
                ' | //iframe | //object | //embed')
_LANGUAGE = re.compile(r'(?:^|\s)(?:language|lang)-([\w+#.-]+)')


@dataclass(frozen=True)
class Extracted:
    markdown: str
    links: list[str]  # Absolute anchor targets in document order, at most LINK_LIMIT.
    links_truncated: bool


class _LinkTargets(HTMLParser):
    """Original anchor targets, queries included (traversal needs ref/utm keys)."""
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.base = url
        self.has_base = False
        self.targets = []
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get('href')
        if not href or len(href) > 4096:
            return
        if tag == 'base' and not self.has_base:
            self.base = urljoin(self.base, href)
            self.has_base = True
        if tag == 'a':
            if len(self.targets) < LINK_LIMIT:
                self.targets.append(href)
            else:
                self.truncated = True


def _code_language(element):
    # Only explicit language-X/lang-X classes (HTML spec convention, used by
    # most Markdown renderers). Site-specific highlighter classes stay out.
    for node in (element, element.find('code')):
        if node is not None:
            match = _LANGUAGE.search(' '.join(node.get('class') or []))
            if match:
                return match[1]
    return ''


class _Converter(MarkdownConverter):
    def convert_img(self, el, text, parent_tags):
        # Inline data: URIs can be megabytes of base64; keep the alt text only.
        if (el.get('src') or '').startswith('data:'):
            return el.get('alt') or ''
        return super().convert_img(el, text, parent_tags)  # pyright: ignore[reportAttributeAccessIssue] — markdownify has no type information.

    def convert_input(self, el, text, parent_tags):
        # Normalization keeps only protected controls; a button-like input's
        # visible value (or accessible label) can carry a qualification.
        if (el.get('type') or '').lower() in ('button', 'submit', 'reset'):
            return el.get('value') or el.get('aria-label') or ''
        return ''

    def convert_button(self, el, text, parent_tags):
        return text if text.strip() else el.get('aria-label') or ''

    def convert_pre(self, el, text, parent_tags):
        # A fence longer than any backtick run inside the code, so a literal
        # ``` line cannot close the block early (evidence.py tracks fences).
        converted = super().convert_pre(el, text, parent_tags)  # pyright: ignore[reportAttributeAccessIssue] — markdownify has no type information.
        longest = max((len(run) for run in re.findall(r'`+', text or '')), default=0)
        if not converted or longest < 3:
            return converted
        fence = '`' * (longest + 1)
        inner = converted.strip('\n')[3:-3]  # 'lang\ncode\n' between ``` markers
        return f'\n\n{fence}{inner}{fence}\n\n'

    def process_text(self, el, parent_tags=None):
        # Literal pipes in cell text (e.g. `x | y` operators) would otherwise
        # split cells. Text nodes only: generated nested-table cells stay intact.
        text = super().process_text(el, parent_tags)  # pyright: ignore[reportAttributeAccessIssue] — markdownify has no type information.
        if parent_tags and parent_tags & {'td', 'th'}:
            text = text.replace('|', r'\|')
        return text

    def convert_dd(self, el, text, parent_tags):
        # Unindented definition bodies: markdownify's ':   ' indentation turns
        # nested headings/paragraphs (e.g. Sphinx member definitions) into
        # indented code, hiding the heading structure evidence.py splits on.
        if '_inline' in parent_tags:
            return ' ' + (text or '').strip() + ' '
        return '\n\n' + (text or '').strip() + '\n\n'


def _markdown(document):
    return _Converter(
        heading_style=ATX, bullets='*', escape_asterisks=False, escape_underscores=False,
        escape_misc=False, strong_em_symbol=UNDERSCORE, code_language_callback=_code_language, table_infer_header=True,
        # wrap=True with no width collapses source line breaks inside prose
        # (one paragraph per line) without rewrapping it.
        wrap=True, wrap_width=None, bs4_options='lxml',
    ).convert(document).strip() + '\n'


def fragment_markdown(fragment: str) -> str:
    """Markdown for a trusted-shape HTML fragment (e.g. generated summaries).

    Plain text without tags passes through unchanged.
    """
    if not re.search(r'<[a-zA-Z][^>]*>', fragment):
        return fragment
    return _markdown(fragment).strip()


def extract(document: str, url: str) -> Extracted:
    """Links come from the unnormalized document; Markdown from main content."""
    links = _LinkTargets(url)
    links.feed(document)
    root = html.document_fromstring(main_content_html(document))
    for element in root.xpath(_NON_CONTENT):
        if element.getparent() is not None:
            element.drop_tree()
    root.make_links_absolute(links.base, resolve_base_href=False, handle_failures='ignore')
    body = root.find('body')
    markdown = _markdown(html.tostring(body if body is not None else root, encoding='unicode'))
    return Extracted(markdown=markdown if markdown.strip() else '',
                     links=[urljoin(links.base, href) for href in links.targets],
                     links_truncated=links.truncated)

