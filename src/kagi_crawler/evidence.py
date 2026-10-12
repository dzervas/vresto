"""Lexical evidence over canonical Markdown; never generate or shorten quotations."""
import base64
from collections import Counter
import hashlib
import json
import math
import re
from urllib.parse import urlsplit


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def evidence_id(source, start, end):
    # Successor .7 API: p1.<base64url compact JSON>, Unicode character offsets,
    # requested URL identity (including query), exact SHA256 canonical version,
    # and quote SHA256. IDs are references, NOT authorization or signed claims.
    # resolve_evidence validates all fields against cache.lookup(url, version);
    # never reacquire/fall back to current content. No separate evidence index:
    # cache version eviction makes every associated ID unavailable coherently.
    payload = [source['requested_url'], source['content_version'], start, end,
               _digest(source['content'][start:end])]
    return 'p1.' + base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False,
        separators=(',', ':')).encode()).decode().rstrip('=')


def resolve_evidence(cache, identifier):
    """Return (canonical_source, start, end), or raise ValueError(code).

    .7 should map invalid_evidence_id to validation failure and
    evidence_unavailable to per-reference unavailable-version outcome. Even a
    syntactically valid forged/changed offset must match the retained quote hash.
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


def sections(text):
    # Keep entire heading sections, code fences and tables, including paragraphs
    # before/after matches. Oversized sections are omitted atomically by the
    # shared budget rather than splitting away negation/warnings. Separate
    # warning/note headings attach to their preceding section as qualifications.
    boundaries = [(0, None, 0)]
    offset = 0
    fence = None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.match(r'(`{3,}|~{3,})', stripped)
        if marker:
            kind = marker[1][0]
            if fence is None:
                fence = kind
            elif fence == kind:
                fence = None
        if fence is None and (match := re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line.rstrip())):
            title = match[1]
            if not re.match(r'(?i)(warning|caution|note|limitations?|exceptions?|compatibility)\b', title):
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
        if text[start:end].strip():
            yield passage_start, end, title
        qualified = bool(re.search(r'(?i)\b(version|only|not|never|warning|caution|experimental|deprecated|except|requires?)\b', text[start:end]))
        ancestors.append((level, start, qualified))


def tokens(text):
    # Identifiers and versions stay whole: connect_safe, foo.bar, v2.4, --flag.
    # No stemming/stopword inference that might collapse a technical constraint.
    return re.findall(r'[\w]+(?:[.:-][\w]+)*', text.casefold())


def select_highlights(data, query):
    key = 'sources' if 'sources' in data else 'results'
    sources = data[key]
    passages = []
    for index, source in enumerate(sources):
        if 'error' in source:
            continue
        text = source['content']
        seen = set()
        for start, end, section in sections(text):
            quote = text[start:end]
            # Exact repeats within one source only. Across sources even identical
            # text retains useful attribution/diversity; near-duplicate strings
            # may differ by one critical version or negation, so never collapse.
            if quote in seen:
                continue
            seen.add(quote)
            passages.append((index, start, end, section, Counter(tokens(quote))))
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
    for score, index, start, end, section in ranked:
        source = sources[index]
        source['relevance'] = 'lexical_match'  # Not a claim the query is answered.
        if selected[index] < 3:
            identifier = evidence_id(source, start, end)
            identity = (source['content_version'], start, end)
            reference = {'url': source['resolved_url'], 'section': section, 'start': start, 'end': end,
                         'evidence_id': identifier}
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
