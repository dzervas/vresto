"""Isolated HTML-only rescore prototype, frozen from evidence.py at c7f1cbca.

No production hooks. All positive BM25 windows (no top32 cutoff) are paired;
only their ordering changes. Real-HTTP/replay and model-free production parity
are hard checks. Keep this copy explicit so a later baseline change cannot
silently change this experiment. PDF evaluation is outside this workload.
"""
from collections import Counter
import math
from kagi_crawler.evidence import (tokens, document_passages, evidence_id,
                                  page_references, pdf_section)

def candidates(data, query):
    key = 'sources' if 'sources' in data else 'results'
    sources = data[key]
    passages = []
    for index, source in enumerate(sources):
        if 'error' in source:
            continue
        text = source['content']
        seen = set()
        for start, end, section in document_passages(source):
            quote = text[start:end]
            # Exact repeats within one source only. Across sources even identical
            # text retains useful attribution/diversity; near-duplicate strings
            # may differ by one critical version or negation, so never collapse.
            if quote in seen:
                continue
            seen.add(quote)
            bag = Counter(tokens(quote))
            # Bare identifier queries also match qualified API names. Retain
            # the full token, and index only identifier suffixes (not versions).
            # Query tokens stay exact: asyncio.TaskGroup must not also match
            # other.TaskGroup just because the final component is shared.
            for term, frequency in list(bag.items()):
                parts = term.split('.')
                if len(parts) > 1 and all(part.isidentifier() for part in parts):
                    bag[parts[-1]] += frequency
            passages.append((index, start, end, section, bag))
    terms = set(tokens(query))
    counts = Counter(term for *_, bag in passages for term in bag)
    average = sum(sum(p[4].values()) for p in passages) / max(1, len(passages))
    ranked = []
    for index, start, end, section, bag in passages:
        score = 0.0
        length = sum(bag.values())
        for term in terms & bag.keys():
            frequency = bag[term]
            inverse = math.log(1 + (len(passages) - counts[term] + 0.5) / (counts[term] + 0.5))
            score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / max(1, average)))
        if score > 0:
            ranked.append((score, index, start, end, section))
    ranked.sort(key=lambda p: (-p[0], p[1], p[2]))
    return ranked, [(0.0, i, a, b, section) for i, a, b, section, _ in passages]


def rescore_order(pool, response):
    """One ordering policy for both measured warm runs and reported evidence."""
    if response['fallback']:
        return pool
    return [p for _, p in sorted(zip(response['scores'], pool),
        key=lambda pair: (-pair[0], pair[1][1], pair[1][2]))]


def select_order(data, ranked):
    key = 'sources' if 'sources' in data else 'results'
    sources = data[key]
    for source in sources:
        if 'error' not in source:
            source['highlights'] = []
            source['content_mode'] = 'highlights'
            source['relevance'] = 'no_lexical_match'
    # Preserve each source's best passages in global BM25 order. The shared byte
    # gate removes lowest-ranked extras fairly across sources before the best.
    # Provisional per-source cap 3 is disclosed; final shared byte
    # gate removes atomic quotes fairly, exposing omissions. No page-count cap.
    selected = Counter()
    duplicates = {}
    windows = set()
    for score, index, start, end, section in ranked:
        source = sources[index]
        if source.get('document_type') == 'pdf':
            # Rank the matching page, but quote previous/current/next canonical
            # pages regardless of lexical overlap: warnings/negation on either
            # side must not disappear. Trusted parser offsets only; full text
            # and expansion's page passages remain unchanged. The shared budget
            # drops this contiguous window atomically, never its qualifications.
            references = source['pdf']['references']
            position = next(i for i, p in enumerate(references) if p['start'] == start)
            start = references[max(0, position - 1)]['start']
            end = references[min(len(references) - 1, position + 1)]['end']
            section = pdf_section(source, start, end)
            if (index, start, end) in windows:
                continue
            windows.add((index, start, end))
        source['relevance'] = 'lexical_match'  # Not a claim the query is answered.
        if selected[index] < 3:
            identifier = evidence_id(source, start, end)
            identity = (source['content_version'], start, end)
            reference = {'url': source['resolved_url'], 'section': section, 'start': start, 'end': end,
                         'evidence_id': identifier, **page_references(source, start, end)}
            # Collapse only identical *whole document versions*, not near-matching
            # quotes from differently qualified documents. Keep each duplicate's
            # attribution and independently resolvable ID for successor expansion.
            if identity in duplicates:
                source.setdefault('duplicate_evidence', []).append(dict(reference,
                    duplicate_of=duplicates[identity]))
            else:
                source['highlights'].append(dict(reference, text=source['content'][start:end]))
                duplicates[identity] = identifier
            selected[index] += 1
        else:
            source['selection_omitted_highlight_count'] = source.get('selection_omitted_highlight_count', 0) + 1
    for source in sources:
        source.pop('content', None)
    data['content_mode'] = 'highlights'
    data['selection'] = {'method': 'cross_document_bm25', 'max_highlights_per_source': 3,
        'relevance_semantics': 'lexical_overlap_not_answer_verification',
        'deduplication': 'exact_canonical_document_versions_with_attributed_references',
        'partial_coverage': any(s.get('selection_omitted_highlight_count') for s in sources)}
    return data


def embedding_order(lexical, windows, response):
    """Predeclared all-window dense and equal-weight RRF(k=60), no thresholds.

    Up to 128 coherent windows/request; no top-K lexical reduction. Overflow
    windows are ineligible for both experimental arms, never clipped. The
    unchanged lexical baseline retains its original candidates. Same three
    quotes/source and atomic wire budgets apply downstream. Scores are sidecar
    diagnostics only; the controlled evidence envelope is not a production API.
    """
    if response['fallback']:
        return lexical, lexical
    eligible = [windows[i] for i in response['eligible']]
    dense = rescore_order(eligible, response)
    identity = lambda p: (p[1], p[2], p[3])
    ranks = {identity(p): 1 / (60 + rank) for rank, p in enumerate(dense, 1)}
    for rank, p in enumerate((p for p in lexical if identity(p) in ranks), 1):
        ranks[identity(p)] += 1 / (60 + rank)
    hybrid = sorted(dense, key=lambda p: (-ranks[identity(p)], p[1], p[2]))
    return dense, hybrid
