"""Conservative semantic UI cleanup and visible document structure."""

import re
from copy import deepcopy

from typing import cast

from lxml import html


_QUALIFICATION = re.compile(
    r'\b(warning|caution|must|requires?|only|not|no|cannot|without|never|unless|except|unsupported|'
    r'deprecated|copyright|licensed?|footnotes?)\b|'
    r'\bdo\s+not\b|\bversion\s+\d', re.I)


def _protected_labels(element):
    # lxml iteration includes comments/processing instructions: their tags are
    # not strings and get() does not honor attribute defaults like elements do.
    return any({'author', 'license'} & set(node.get('rel', '').lower().split())
               or node.tag == 'time' or node.get('aria-controls')
               or ((node.tag in {'button', 'input', 'select'} or node.get('role') == 'button')
                   and any(parent.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
                           for parent in node.iterancestors()))
               or any(_QUALIFICATION.search(node.get(attribute, ''))
                      for attribute in ('value', 'aria-label'))
               for node in element.iter() if isinstance(node.tag, str))


def _navigation_only(element):
    # A semantic boundary is evidence, not permission to discard prose. Notes,
    # code and version constraints can live outside <main>, even inside <nav>.
    if _QUALIFICATION.search(element.text_content()):
        return False
    if _protected_labels(element):
        return False
    if element.xpath('.//pre | .//code | .//table | .//p | .//blockquote'):
        return False
    copy = deepcopy(element)
    for child in copy.xpath('.//a | .//button | .//input | .//select | '
                            './/h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6'):
        if child.getparent() is not None:
            child.drop_tree()
    return not copy.text_content().strip(' \t\n\r|•·›»/:-')


def _prose_context(element):
    return any(parent.tag in {'p', 'pre', 'code', 'table', 'blockquote', 'dt'}
               for parent in element.iterancestors())


def _clean_controls(root):
    # Actual interactive semantics, not CSS names or words such as "reaction",
    # distinguish UI from prose. Embedded examples and qualified labels survive.
    for element in root.xpath('//button | //*[@role="button"] | //select | '
                              '//input[@type="button" or @type="submit" or @type="reset"]'):
        # Check the original root as well as descendants before retagging the
        # copy: author/license links and timestamps are evidence, even when a
        # publisher also gives them interactive semantics. Accessible labels and
        # visible input values may contain constraints absent from text_content.
        if _protected_labels(element):
            # Heading controls carry visible document/release labels. A bound
            # accordion control labels content that survives acquisition; keep
            # uncertain bindings too rather than silently detaching that scope.
            continue
        control = deepcopy(element)
        control.tag = 'button'
        container = html.Element('div')
        container.append(control)
        if (element.getparent() is not None and not _prose_context(element)
                and not re.search(r'[.!?]\s*$', element.text_content())
                and _navigation_only(container)):
            element.drop_tree()
    for element in root.xpath('//img[@role="presentation" or @role="none" or @aria-hidden="true"]'):
        if not _prose_context(element) and not _QUALIFICATION.search(element.get('alt', '')):
            element.drop_tree()
    for element in root.xpath('//a[@href]'):
        # An explicitly labelled permalink's punctuation marker inside a
        # heading/signature is decoration. Unlabelled fragment references may
        # be footnotes, even when their visible label is only punctuation.
        label = element.get('aria-label') or element.get('title', '')
        if (re.fullmatch(r'(?i)(?:permalink(?: to this (?:heading|definition))?|link to this (?:heading|definition))', label)
                and element.get('href', '').startswith('#')
                and element.text_content().strip() in {'¶', '§', '#', '🔗'}
                and any(parent.tag in {'dt', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
                        for parent in element.iterancestors())
                and not any(parent.tag in {'pre', 'code', 'table', 'blockquote'}
                            for parent in element.iterancestors())
                and not element.get('rel')):
            element.drop_tree()


def _structural_headings(root):
    # This is a documentation-generator convention, not a host/site rule:
    # Sphinx emits the same definition markup across unrelated publishers.
    # Keep it generic and structurally guarded rather than matching domains.
    # Sphinx signatures combine a definition pair, a signature marker and an
    # identifier. Neither a coincidentally named class nor an arbitrary glossary
    # term is sufficient. Keep the visible signature, including inline markup.
    definitions = set()
    for term in root.xpath('//dl/dt[@id]'):
        classes = set(term.get('class', '').split())
        if ({'sig', 'sig-object'} <= classes
                and term.getnext() is not None and term.getnext().tag == 'dd'
                and (term.getprevious() is None or term.getprevious().tag != 'dt')
                and term.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " sig-name ")]')):
            definitions.add(term)
    # Discussion headers already containing a heading need no promotion. Use
    # visible attribution/date labels only; never synthesize "Comment N" titles.
    headers = set()
    for article in root.xpath('//article | //*[@role="article"]'):
        if article.xpath('./h1 | ./h2 | ./h3 | ./h4 | ./h5 | ./h6 | ./header/*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::h6]'):
            continue
        for header in article.xpath('./header'):
            if (header.text_content().strip() and len(header.text_content()) <= 240
                    and header.xpath('.//time | .//*[contains(concat(" ", normalize-space(@rel), " "), " author ")]')
                    and not _QUALIFICATION.search(header.text_content())
                    and not header.xpath('.//pre | .//code | .//table | .//blockquote | .//dl')
                    and all(child.tag in {'a', 'span', 'time', 'p', 'div', 'strong', 'em', 'img'}
                            for child in header.iterdescendants())):
                headers.add(header)
                break
    headings = []
    definition_levels = {}
    article_levels = {}
    for element in root.iter():
        if element in definitions or element in headers:
            ancestors = set(element.iterancestors())
            # A sibling's local headings do not establish the next boundary's
            # ancestry. Headings within the same containing article still do.
            level = next((rank for heading, rank in reversed(headings)
                          if all(parent in ancestors for parent in heading.iterancestors()
                                 if parent.tag in {'article', 'dl'} or parent.get('role') == 'article')), 1)
        if element in definitions:
            definition = element.getparent()
            if definition not in definition_levels:
                outer = next((parent for parent in definition.iterancestors()
                              if parent in definition_levels), None)
                rank = definition_levels[outer] + 1 if outer is not None else level + 1
                if rank > 6:
                    continue  # Flattening would detach the enclosing scope.
                definition_levels[definition] = rank
            element.tag = f'h{definition_levels[definition]}'
        elif element in headers:
            article = element.getparent()
            outer = next((parent for parent in article.iterancestors()
                          if parent in article_levels), None)
            rank = article_levels[outer] + 1 if outer is not None else level + 1
            if rank > 6:
                continue
            article_levels[article] = rank
            element.tag = f'h{article_levels[article]}'
            for child in element.iterdescendants():
                if child.tag in {'p', 'div'}:
                    child.tag = 'span'
                    # Preserve visible separation when block attribution labels
                    # become an inline heading (Alice + date must not fuse).
                    child.text = ' ' + (child.text or '')
                    child.tail = ' ' + (child.tail or '')
        elif element.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            headings.append((element, int(element.tag[1])))


def main_content_html(document: str) -> str:
    """Keep uncertain boundaries verbatim; never synthesize or summarize text.

    This precedes Markdown conversion (extraction.py) and the content hash.
    Cached canonical versions are deliberately never re-normalized on read, so
    old evidence IDs retain their original text and character offsets.
    """
    root = html.document_fromstring(document)
    _clean_controls(root)
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
    _structural_headings(root)
    return cast(str, html.tostring(root, encoding='unicode'))
