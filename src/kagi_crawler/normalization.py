"""Conservative, model-free removal of explicit document navigation."""
import re
from copy import deepcopy

from lxml import html


_QUALIFICATION = re.compile(
    r'\b(warning|caution|must|requires?|only|not|never|unless|except|unsupported|'
    r'deprecated|copyright|licensed?|footnotes?)\b|'
    r'\bdo\s+not\b|\bversion\s+\d', re.I)


def _navigation_only(element):
    # A semantic boundary is evidence, not permission to discard prose. Notes,
    # code and version constraints can live outside <main>, even inside <nav>.
    if _QUALIFICATION.search(element.text_content()):
        return False
    if any({'author', 'license'} & set(link.get('rel', '').lower().split())
           for link in element.xpath('.//a[@rel]')):
        return False
    if element.xpath('.//pre | .//code | .//table | .//p | .//blockquote'):
        return False
    copy = deepcopy(element)
    for child in copy.xpath('.//a | .//button | .//input | .//select | '
                            './/h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6'):
        if child.getparent() is not None:
            child.drop_tree()
    return not copy.text_content().strip(' \t\n\r|•·›»/:-')


def main_content_html(document):
    """Keep uncertain boundaries verbatim; never synthesize or summarize text.

    This precedes the existing Crawl4AI Markdown conversion and content hash.
    Cached canonical versions are deliberately never re-normalized on read, so
    old evidence IDs retain their original text and character offsets.
    """
    root = html.document_fromstring(document)
    for element in root.xpath('//nav | //*[@role="navigation" or @role="toolbar"]'):
        if element.getparent() is not None and _navigation_only(element):
            element.drop_tree()
    # Only link-list footers are chrome. Attribution prose, document notes and
    # fragment references remain, including when they are outside <main>.
    for element in root.xpath('//footer | //*[@role="contentinfo"]'):
        links = element.xpath('.//a[@href]')
        if (element.getparent() is not None and len(links) >= 2
                and all(not link.get('href').startswith('#') for link in links)
                and _navigation_only(element)):
            element.drop_tree()
    # An index can consist entirely of navigation. Do not turn a useful but
    # heading-poor document into an empty/unsupported acquisition.
    remaining = ''.join(root.xpath('//body//text()[not(ancestor::script) and not(ancestor::style) '
                                   'and not(ancestor::h1 or ancestor::h2 or ancestor::h3 '
                                   'or ancestor::h4 or ancestor::h5 or ancestor::h6)]'))
    if not remaining.strip():
        return document
    return html.tostring(root, encoding='unicode')
