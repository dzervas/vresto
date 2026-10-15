"""Lexical evidence over canonical Markdown; never generate or shorten quotations."""
from collections.abc import Iterator, Iterable
from typing import cast
import base64
from collections import Counter
import hashlib
import json
import math
import re
from urllib.parse import urlsplit


from .cache import DocumentCache
from .types import ToolData, ToolResult, SourceSuccess

def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def evidence_id(source: SourceSuccess, start: int, end: int) -> str:
    # Evidence ID format: p1.<base64url compact JSON>, Unicode character offsets,
    # requested URL identity (including query), exact SHA256 canonical version,
    # and quote SHA256. IDs are references, NOT authorization or signed claims.
    # resolve_evidence validates all fields against cache.lookup(url, version);
    # never reacquire/fall back to current content. No separate evidence index:
    # cache version eviction makes every associated ID unavailable coherently.
    payload = [source['requested_url'], source['content_version'], start, end,
               _digest(source['content'][start:end])]
    return 'p1.' + base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False,
        separators=(',', ':')).encode()).decode().rstrip('=')


def resolve_evidence(cache: DocumentCache, identifier: str) -> tuple[SourceSuccess, int, int]:
    """Return (canonical_source, start, end), or raise ValueError(code).

    invalid_evidence_id denotes malformed references; evidence_unavailable denotes
    missing versions or mismatched quotes. Even a syntactically valid forged/changed
    offset must match the retained quote hash.
    This pure lookup never warms/acquires a replacement document.
    """
    try:
        if not isinstance(identifier, str) or len(identifier) > 32768 or not identifier.startswith('p1.'):
            raise ValueError
        encoded = identifier[3:]
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 5:
            raise ValueError
        url, version, start, end, quote_hash = values
        if (not isinstance(url, str) or len(url) > 4096 or not url.startswith(('http://', 'https://')) or
            not isinstance(version, str) or not re.fullmatch('[0-9a-f]{64}', version) or
            not isinstance(quote_hash, str) or not re.fullmatch('[0-9a-f]{64}', quote_hash) or
            type(start) is not int or type(end) is not int or not 0 <= start < end):
            raise ValueError
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or any(ord(c) <= 32 for c in url):
            raise ValueError
        canonical = base64.urlsafe_b64encode(json.dumps(values, ensure_ascii=False,
            separators=(',', ':')).encode()).decode().rstrip('=')
        if encoded != canonical:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('invalid_evidence_id') from None
    source = cache.lookup(url, version)
    if source is None:
        raise ValueError('evidence_unavailable')
    text = source.get('content', '')
    if (not isinstance(text, str) or end > len(text) or _digest(text) != version or
            _digest(text[start:end]) != quote_hash):
        raise ValueError('invalid_evidence_id')
    return source, start, end


def document_passages(source: SourceSuccess) -> Iterable[tuple[int, int, str | None]]:
    # PDF text may itself contain Markdown-looking headings. Only trusted
    # processor page offsets define PDF passages, never untrusted text markers.
    if source.get('document_type') == 'pdf':
        assert 'pdf' in source  # PDF acquisitions always carry parser metadata.
        return ((p['start'], p['end'], f"PDF page {p['page']}")
            for p in source['pdf']['references'] if p['status'] == 'text')
    return sections(source['content'])


def page_references(source: SourceSuccess, start: int, end: int) -> dict[str, list[int]]:
    if source.get('document_type') != 'pdf':
        return {}
    assert 'pdf' in source  # PDF acquisitions always carry parser metadata.
    return {'pages': [p['page'] for p in source['pdf']['references']
        if p['start'] < end and p['end'] > start]}


def pdf_section(source: SourceSuccess, start: int, end: int) -> str:
    pages = page_references(source, start, end)['pages']
    return f'PDF page {pages[0]}' if len(pages) == 1 else f'PDF pages {pages[0]}–{pages[-1]}'


def expand_evidence(cache: DocumentCache, identifiers: list[str]) -> ToolResult:
    """Resolve all references before returning anything; malformed IDs fail the request.

    Unlike Exa's URL-based follow-up contents workflow, these IDs point to retained
    canonical versions, not a promise of fresh/live contents or archive parity.
    Live Exa access was unavailable; this is an ergonomics/source-shape comparison
    only. Adjacent complete sections add context; the shared gate drops context
    before quotes, never slicing a warning or silently replacing historical text.
    """
    sources = []
    for identifier in identifiers:
        try:
            canonical, start, end = resolve_evidence(cache, identifier)
        except ValueError as error:
            if str(error) != 'evidence_unavailable':
                raise
            sources.append({'evidence_id': identifier, 'error': {
                'code': 'evidence_unavailable', 'message': 'Referenced canonical version is not retained.'}})
            continue
        text = canonical['content']
        passages = list(document_passages(canonical))
        section = (pdf_section(canonical, start, end) if canonical.get('document_type') == 'pdf' else
                   next((title for a, b, title in passages if a == start and b == end), None))
        reference = {'url': canonical['resolved_url'], 'section': section,
                     'start': start, 'end': end, 'evidence_id': identifier,
                     **page_references(canonical, start, end)}
        before = [p for p in passages if p[1] <= start]
        after = [p for p in passages if p[0] >= end]
        neighbors = before[-1:] + after[:1]
        source = {k: v for k, v in canonical.items() if k != 'content'}
        source.update(cache_state='retained_version', content_mode='evidence_expansion',
            highlights=[dict(reference, text=text[start:end])],
            context=[{'url': canonical['resolved_url'], 'section': title, 'start': a,
                      'end': b, 'text': text[a:b],
                      **page_references(canonical, a, b)} for a, b, title in neighbors])
        sources.append(source)
    return {'sources': sources, 'content_mode': 'evidence_expansion', 'expansion': {
        'version_policy': 'exact_retained_version_no_acquisition',
        'context_policy': 'preceding_and_following_complete_sections',
        'budget_policy': 'omit_context_sections_then_whole_sources_preserve_quote_and_reference'}}


_QUALIFICATION = re.compile(r'(?i)(warning|caution|note|limitations?|exceptions?|compatibility)\b')


def _heading_title(line):
    match = re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line.rstrip())
    if match:
        # Scope and reference labels describe visible heading text, not numeric
        # releases (or qualification words) in destinations/optional link titles.
        return re.sub(r'\[([^\]]*)\]\(\S+?(?:\s+["\'][^"\']*["\'])?\)', r'\1', match[1])
    return None


def _fence_transition(line, fence):
    marker = re.match(r'(`{3,}|~{3,})', line.lstrip())
    if marker:
        if fence is None:
            return marker[1]
        if marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
            return None
    return fence


def markdown_blocks(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """Canonical block offsets, including single-newline rendered prose.

    Complete sentence-ending prose lines are boundaries, but wrapped/structural
    lines are not. Blank lines in fenced code do not divide the code. Headings
    stay with their following text; oversized prose lines remain atomic.
    """
    position = block_start = start
    fence = None
    heading_pending = False
    for line in text[start:end].splitlines(keepends=True):
        fence = _fence_transition(line, fence)
        if re.match(r'^#{1,6}\s', line):
            heading_pending = True
        elif line.strip():
            heading_pending = False
        position += len(line)
        prose_end = (re.search(r'[.!?][\"\u201d\u2019\')\]]*\s*$', line) and
                     not re.match(r'^(?: {4}|\t|\s*(?:[|>#`~]|[-*+]\s|\d+[.)]\s))', line))
        if (not line.strip() or prose_end) and fence is None and not heading_pending:
            if text[block_start:position].strip():
                yield block_start, position
            block_start = position
    if text[block_start:end].strip():
        yield block_start, end


def passage_windows(text: str, start: int, end: int, *, include_attached_qualification: bool = True) -> Iterator[tuple[int, int]]:
    """Coherent contiguous windows retaining both neighboring block groups.

    Neighbors are included even without query overlap: warnings and release
    caveats must not disappear because they score poorly. Attached qualification
    headings extend preceding windows through their complete body. These are
    canonical slices, not prefix cuts or reconstructed quotes. 400 characters is a grouping
    target, not a truncation limit; oversized blocks and the shared byte gate
    remain atomic. Sections up to 1200 characters need no finer windows.
    """
    if end - start <= 1200:
        yield start, end
        return
    groups = []
    attached = False
    fence = None
    for a, b in markdown_blocks(text, start, end):
        # Qualification headings are part of this section, not new sections.
        # Keep their entire body with the preceding claim block: no window
        # containing that claim may stop halfway through its attached warning.
        # An unpunctuated explanation may share a block with the heading.
        # Inspect every heading outside code, not just the block's first line.
        for line in text[a:b].splitlines():
            fence = _fence_transition(line, fence)
            if fence is None and _QUALIFICATION.match(_heading_title(line) or ''):
                attached = True
        if groups and (attached or b - groups[-1][0] <= 400):
            groups[-1] = (groups[-1][0], b)
        else:
            groups.append((a, b))
    seen = set()
    for i in range(len(groups)):
        # A Warning heading qualifies preceding claims in this section even
        # across intervening explanations, not only the immediately prior block.
        # Extend contiguously through its complete body; an oversized qualified
        # quote is omitted atomically. Do not use this extension to classify a
        # distant local warning as an introductory scope for child sections.
        window = (groups[max(0, i - 1)][0],
                  end if attached and include_attached_qualification else groups[min(len(groups) - 1, i + 1)][1])
        if window not in seen:
            seen.add(window)
            yield window


def sections(text: str) -> Iterator[tuple[int, int, str | None]]:
    # Small sections and explicitly scoped ancestry remain atomic. Long ordinary
    # sections use coherent block windows rather than almost-whole-page quotes.
    # Separate warning/note headings still attach to their preceding section.
    boundaries: list[tuple[int, str | None, int]] = [(0, None, 0)]
    offset = 0
    fence = None
    for line in text.splitlines(keepends=True):
        fence = _fence_transition(line, fence)
        if fence is None and (title := _heading_title(line)) is not None:
            if not _QUALIFICATION.match(title):
                boundaries.append((offset, title, len(line) - len(line.lstrip('#'))))
        offset += len(line)
    ancestors = []
    for i, (start, title, level) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        while ancestors and ancestors[-1][0] >= level:
            ancestors.pop()
        # A scoped parent introduction must accompany its child claim. A single
        # contiguous canonical slice also retains intervening material: prefer
        # excess context / explicit budget omission over a misleading small quote.
        scoped = [position for _, position, qualified in ancestors if qualified]
        passage_start = min([start, *scoped])
        # Conventional release headings and nearby introductory constraints are
        # scopes, not just section labels. Inspect the first complete window:
        # later local warnings must not scope every descendant of a long section.
        _, introduction_end = next(passage_windows(text, start, end,
            include_attached_qualification=False), (start, end))
        # Issue references (#108951) and bare numbers are not release scopes.
        # Attributed discussion headings also contain ordinals/dates (and may
        # contain version-like author names), not a release for their children.
        release_title = bool(title and not re.match(r'(?i)^(?:Comment|Description) \d+ — ', title) and
            re.search(r'(?i)\b(?:version|release)\b|(?<![\w.#])(?:v\d+(?:\.\d+)*|\d+\.\d+(?:\.\d+)*)(?:[-+][\w.]+)?(?![\w.])', title))
        qualified = bool(re.search(r'(?i)\b(version|only|not|never|warning|caution|experimental|deprecated|except|requires?)\b', text[start:introduction_end]) or
            release_title)
        if text[start:end].strip():
            if scoped:
                # Parent scopes cannot be stitched onto a later quote: retain
                # the contiguous range, or disclose a budget omission.
                yield passage_start, end, title
            else:
                for a, b in passage_windows(text, start, end):
                    # A local scoped introduction can end at a complete block
                    # window, but must begin at that introduction. Distant claims
                    # may still be oversized; never fabricate a stitched quote.
                    yield start if qualified else a, b, title
        ancestors.append((level, start, qualified))


def tokens(text: str) -> list[str]:
    # Identifiers and versions stay whole: connect_safe, foo.bar, v2.4, --flag.
    # No stemming/stopword inference that might collapse a technical constraint.
    return re.findall(r'[\w]+(?:[.:-][\w]+)*', text.casefold())


def _passage_bag(quote):
    bag = Counter(tokens(quote))
    # Only bare identifier suffixes are aliases. Qualified query tokens remain
    # exact: other.TaskGroup cannot satisfy asyncio.TaskGroup.
    for term, frequency in list(bag.items()):
        parts = term.split('.')
        if len(parts) > 1 and all(part.isidentifier() for part in parts):
            bag[parts[-1]] += frequency
    return bag


_MARKDOWN_LINK = re.compile(r'(?<!!)\[((?:\\.|[^\]\\])*)\]\((?:\\.|[^)\\])*\)')


def _link_density_penalty(quote):
    # Related-page lists, docs sidebars and forum topic tables repeat query
    # terms without answering them. Measure visible link labels, not URL bytes:
    # long destinations must not disguise a navigation-heavy passage as prose.
    # Markdown examples in code are text, not navigational links. Mask only
    # brackets so their ordinary character contribution remains in the share.
    lines = []
    fence = None
    for line in quote.splitlines(keepends=True):
        previous = fence
        fence = _fence_transition(line, fence)
        if previous is not None or fence is not None:
            line = line.replace('[', ' ').replace(']', ' ')
        else:
            line = re.sub(r'(`+)(.*?)\1',
                lambda match: match[0].replace('[', ' ').replace(']', ' '), line)
        lines.append(line)
    quote = ''.join(lines)
    # Linked avatars must not leave image destination bytes in the denominator.
    quote = re.sub(r'!\[((?:\\.|[^\]\\])*)\]\((?:\\.|[^)\\])*\)', r'\1', quote)
    links = list(_MARKDOWN_LINK.finditer(quote))
    # Ignore layout punctuation/whitespace: Markdown table pipes and bullets
    # must not dilute the link share of a Related topics table or sidebar.
    linked = sum(sum(char.isalnum() for char in link[1]) for link in links)
    visible_text = _MARKDOWN_LINK.sub(r'\1', quote)
    visible = sum(char.isalnum() for char in visible_text)
    # A reference list or API index can itself be the answer. Penalize, never
    # filter it; the floor keeps fully linked answers eligible for admission.
    # Leave ordinary citations alone up to 40%; ramp to the floor at 58%.
    return max(0.1, min(1, 1 - (linked / max(1, visible) - 0.4) / 0.2))


def _qualifications(quote):
    # Conservative lexical guard, not interpretation of a claim. Distinct
    # negations, warnings and release constraints must survive overlap removal.
    return {part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+', quote.casefold())
            if re.search(r'\b(not|never|no|neither|cannot|without|only|warning|caution|notes?|'
                         r'exceptions?|compatibility|except|unless|requires?|must|shall|'
                         r'mandatory|required|unsupported|limitations?|deprecated|'
                         r'experimental|version|uncancel)\b|'
                         r'\b(?:don|doesn|isn|aren|can|won|shouldn|mustn)[\'’]t\b|'
                         r'(?<!\w)(?:v\d+|\d+\.\d+)', part)}


def _redundant(candidate, selected):
    for previous in selected:
        # Offsets have meaning only inside this exact source and version.
        if candidate['index'] != previous['index'] or candidate['version'] != previous['version']:
            continue
        overlap = max(0, min(candidate['end'], previous['end']) -
                      max(candidate['start'], previous['start']))
        # Sliding windows without new terms or constraints do not justify
        # growing a selected quote merely to include unrelated edge context.
        if (overlap > 0 and
                candidate['terms'] <= previous['terms'] and
                candidate['qualifications'] <= previous['qualifications']):
            return True
    return False


def select_highlights(data: ToolData, query: str, limit: int | None = None) -> ToolData:
    from copy import deepcopy
    from .budget import apply_budget

    # Acquisition/cache dictionaries and canonical text are never changed by
    # selection, including unsuccessful admission probes.
    data = deepcopy(data)
    key = 'sources' if 'sources' in data else 'results'
    sources = data[key]
    terms = set(tokens(query))
    passages = []
    for index, source in enumerate(sources):
        if 'error' in source:
            continue
        text = source['content']
        seen = set()
        for start, end, section in document_passages(source):
            # Rank the matching page itself, not a neighbor's borrowed match.
            bag = _passage_bag(text[start:end])
            if source.get('document_type') == 'pdf':
                if not terms & bag.keys():
                    continue
                # Trusted previous/current/next pages are a single atomic quote.
                references = source['pdf']['references']
                position = next(i for i, p in enumerate(references) if p['start'] == start)
                start = references[max(0, position - 1)]['start']
                end = references[min(len(references) - 1, position + 1)]['end']
                section = pdf_section(source, start, end)
            quote = text[start:end]
            if quote in seen:
                continue
            seen.add(quote)
            passages.append(dict(index=index, start=start, end=end, section=section,
                bag=bag, matching_terms=terms & bag.keys(),
                terms=terms & (_passage_bag(quote).keys() if source.get('document_type') == 'pdf' else bag.keys()),
                version=source['content_version'],
                link_density_penalty=_link_density_penalty(quote) if source.get('document_type') != 'pdf' else 1,
                qualifications=_qualifications(quote)))
    counts = Counter(term for p in passages for term in p['bag'])
    average = sum(sum(p['bag'].values()) for p in passages) / max(1, len(passages))
    ranked = []
    for passage in passages:
        bag = passage['bag']
        length = sum(bag.values())
        score = 0.0
        for term in sorted(passage['matching_terms']):
            frequency = bag[term]
            inverse = math.log(1 + (len(passages) - counts[term] + 0.5) / (counts[term] + 0.5))
            score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / max(1, average)))
        # A giant background window should not win merely by repeating a name.
        score /= math.sqrt(max(1, length / 200))
        if score > 0:
            passage['score'] = score
            ranked.append(passage)
    data_original_texts = [s.get('content', '') for s in sources]
    totals = Counter(p['index'] for p in ranked)
    for index, source in enumerate(sources):
        source.pop('content', None)
        if 'error' not in source:
            source.update(highlights=[], content_mode='highlights',
                relevance='lexical_match' if totals[index] else 'no_lexical_match')
            if totals[index]:
                source['selection_omitted_highlight_count'] = totals[index]
    data['content_mode'] = 'highlights'
    data['selection'] = {'method': 'bm25_marginal_query_coverage', 'max_highlights_per_source': 3,
        'relevance_semantics': 'lexical_overlap_not_answer_verification',
        'deduplication': 'same_source_version_overlap_and_exact_versions_with_attributed_references',
        'query_terms': sorted(terms), 'covered_query_terms': [],
        'uncovered_query_terms': sorted(terms), 'partial_coverage': bool(terms)}
    selected = []
    selected_counts = Counter()
    covered = set()
    admitted = []
    maximum = max((p['score'] for p in ranked), default=1)

    def update_selection(output, coverage):
        output['selection'].update(covered_query_terms=sorted(coverage),
            uncovered_query_terms=sorted(terms - coverage),
            partial_coverage=bool(terms - coverage) or any(
                s.get('selection_omitted_highlight_count') for s in output[key]))

    def fits(output):
        if limit is None:
            return True
        bounded = apply_budget(output, limit)
        # The final gate may disclose title/snippet reductions or omit sources
        # without selected evidence. Keep the full internal source list so its
        # original indices/counts remain truthful at final serialization.
        if 'error' in bounded:
            return False
        omitted = set(bounded['budget']['omitted_source_indices'])
        retained = iter(bounded[key])
        for index, source in enumerate(output[key]):
            if index in omitted:
                if source.get('highlights') or source.get('duplicate_evidence'):
                    return False
                continue
            returned = next(retained)
            # Every admitted quote and attributed reference must survive intact;
            # no allowance for final-gate quote loss or changed qualification.
            if any(returned.get(field) != value for field, value in source.items()
                   if field not in ('title', 'provider_snippet', 'truncated_fields')):
                return False
        return True

    def overlaps(candidate, previous):
        return (candidate['index'] == previous['index'] and
                candidate['version'] == previous['version'] and
                candidate['start'] < previous['end'] and previous['start'] < candidate['end'])

    def render_selection(output, selection):
        # Rebuild attribution as well as quotes: replacing an owning range must
        # not leave another source's duplicate_of pointing at its old range.
        duplicates = {}
        for source in output[key]:
            if 'error' not in source:
                source['highlights'] = []
                source.pop('duplicate_evidence', None)
        for passage in selection:
            index = passage['index']
            source = output[key][index]
            start, end = passage['start'], passage['end']
            canonical = cast(SourceSuccess, {**source, 'content': data_original_texts[index]})
            identifier = evidence_id(canonical, start, end)
            reference = {'url': source['resolved_url'], 'section': passage['section'],
                         'start': start, 'end': end, 'evidence_id': identifier,
                         **page_references(source, start, end)}
            identity = (source['content_version'], start, end)
            if identity in duplicates:
                source.setdefault('duplicate_evidence', []).append(dict(reference,
                    duplicate_of=duplicates[identity]))
            else:
                source['highlights'].append(dict(reference, text=canonical['content'][start:end]))
                duplicates[identity] = identifier

    # First admit one useful fitting candidate from each source, then extras.
    # A nonfitting best candidate never prevents trying its smaller alternatives.
    for first_pass in (True, False):
        cap = 1 if first_pass else 3
        pending = [p for p in ranked if p not in admitted and
                   (selected_counts[p['index']] < cap or
                    (not first_pass and any(overlaps(p, old) for old in selected)))]
        deferred = []
        rerank = True
        while pending:
            # Marginal rank changes only when admitted evidence covers new
            # terms. Reverse sorting permits constant-time best-candidate pops.
            if rerank:
                # Penalize marginal coverage too: sidebars often mention more
                # distinct query terms than any individual answer paragraph.
                pending = sorted(pending, key=lambda p: (
                    -((p['score'] / maximum + len(p['terms'] - covered)) * p['link_density_penalty']),
                    p['index'], p['start'], p['end']), reverse=True)
                rerank = False
            candidate = pending.pop()
            index = candidate['index']
            if _redundant(candidate, selected):
                # Only count suppression once, on the final extra pass.
                if not first_pass:
                    trial = deepcopy(data)
                    source = trial[key][index]
                    source['selection_omitted_highlight_count'] -= 1
                    source['redundant_highlight_count'] = source.get('redundant_highlight_count', 0) + 1
                    update_selection(trial, covered)
                    # Suppression counters can grow after the last admission.
                    # If their metadata cannot fit without dropping a quote,
                    # leave this candidate counted as an ordinary omission.
                    if fits(trial):
                        data = trial
                        sources = data[key]
                continue
            replaced = [old for old in selected if overlaps(candidate, old)]
            replacement = candidate
            if replaced:
                # Sliding documentation windows may each carry a distinct
                # warning at opposite ends. Their contiguous union retains all
                # qualifications, not just the overlapping query terms. Even a
                # small overlap is packed once rather than emitted twice.
                start = min(p['start'] for p in [candidate, *replaced])
                end = max(p['end'] for p in [candidate, *replaced])
                quote = data_original_texts[index][start:end]
                # Expansion labels exact known ranges, not synthetic unions.
                section = candidate['section']
                if sources[index].get('document_type') == 'pdf':
                    section = pdf_section(sources[index], start, end)
                elif (start, end) != (candidate['start'], candidate['end']):
                    section = next((p['section'] for p in replaced
                                    if (p['start'], p['end']) == (start, end)), None)
                replacement = dict(candidate, start=start, end=end, section=section,
                    terms=terms & _passage_bag(quote).keys(), qualifications=_qualifications(quote))
            trial_selected = [p for p in selected if p not in replaced]
            # Keep the first selected slot stable when a later window replaces it.
            position = selected.index(replaced[0]) if replaced else len(trial_selected)
            trial_selected.insert(position, replacement)
            trial = deepcopy(data)
            target = trial[key][index]
            target['selection_omitted_highlight_count'] -= 1
            if replaced:
                target['redundant_highlight_count'] = target.get('redundant_highlight_count', 0) + len(replaced)
            trial_covered = set().union(*(p['terms'] for p in trial_selected))
            render_selection(trial, trial_selected)
            update_selection(trial, trial_covered)
            # Recount the complete wire shape, including regenerated IDs and
            # duplicate references. Reject the union atomically if it cannot fit;
            # never trade an admitted warning for a smaller replacement quote.
            if not fits(trial):
                continue
            data = trial
            sources = data[key]
            selected = trial_selected
            admitted.append(candidate)
            selected_counts[index] += 1 - len(replaced)
            rerank = bool(trial_covered - covered)
            covered = trial_covered
            if selected_counts[index] < cap:
                resumed = [p for p in deferred if p['index'] == index]
                pending.extend(resumed)
                deferred = [p for p in deferred if p['index'] != index]
                rerank = rerank or bool(resumed)
            if selected_counts[index] == cap:
                # At the cap, replacements can still free slots. Defer disjoint
                # extras so merging two selected windows can admit one later.
                capped = [p for p in pending if p['index'] == index and
                          (first_pass or not any(overlaps(p, old) for old in selected))]
                deferred.extend(capped)
                pending = [p for p in pending if p['index'] != index or
                           (not first_pass and any(overlaps(p, old) for old in selected))]
    update_selection(data, covered)
    return data
