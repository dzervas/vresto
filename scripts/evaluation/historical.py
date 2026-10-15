"""Historical replay helpers copied verbatim from c7f1cbca.

This is the dependency closure of selection.py's archived candidates/select_order
and run.py's finalizer: segmentation/tokenization, evidence references, recursive
safety filtering, and the complete MCP byte-budget gate. All 48 archived HTTP
digests match this revision. Do not import production helpers or update this copy
when production evolves. highlights.py and freeze.py intentionally use current
production instead. MCP serialization still uses the project's locked dependency.
"""
import base64
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import unquote

from mcp.types import CallToolResult, TextContent



# From src/kagi_crawler/evidence.py at c7f1cbca.


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


def document_passages(source):
    # PDF text may itself contain Markdown-looking headings. Only trusted
    # processor page offsets define PDF passages, never untrusted text markers.
    if source.get('document_type') == 'pdf':
        return ((p['start'], p['end'], f"PDF page {p['page']}")
            for p in source['pdf']['references'] if p['status'] == 'text')
    return sections(source['content'])


def page_references(source, start, end):
    if source.get('document_type') != 'pdf':
        return {}
    return {'pages': [p['page'] for p in source['pdf']['references']
        if p['start'] < end and p['end'] > start]}


def pdf_section(source, start, end):
    pages = page_references(source, start, end)['pages']
    return f'PDF page {pages[0]}' if len(pages) == 1 else f'PDF pages {pages[0]}–{pages[-1]}'


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


def markdown_blocks(text, start, end):
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


def passage_windows(text, start, end, *, include_attached_qualification=True):
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


def sections(text):
    # Small sections and explicitly scoped ancestry remain atomic. Long ordinary
    # sections use coherent block windows rather than almost-whole-page quotes.
    # Separate warning/note headings still attach to their preceding section.
    boundaries = [(0, None, 0)]
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
        qualified = bool(re.search(r'(?i)\b(version|only|not|never|warning|caution|experimental|deprecated|except|requires?)\b', text[start:introduction_end]) or
            (title and re.search(r'(?i)(?<!\w)v?\d+(?:\.\d+)*(?:[-+][\w.]+)?(?!\w)', title)))
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


def tokens(text):
    # Identifiers and versions stay whole: connect_safe, foo.bar, v2.4, --flag.
    # No stemming/stopword inference that might collapse a technical constraint.
    return re.findall(r'[\w]+(?:[.:-][\w]+)*', text.casefold())


# From src/kagi_crawler/safety.py at c7f1cbca.


def contains_secret(value, secrets):
    if isinstance(value, str):
        # URLs can reflect percent-encoded credentials, including repeated encoding.
        for _ in range(4):
            if any(secret and secret in value for secret in secrets):
                return True
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
    elif isinstance(value, dict):
        return any(contains_secret(k, secrets) or contains_secret(v, secrets)
                   for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        return any(contains_secret(v, secrets) for v in value)
    return False


def safe_sources(data, secrets):
    # Do not redact text in place: that would break quote/version fidelity. Failed
    # outcomes intentionally omit contaminated URLs and nested errors as well.
    for key in ('sources', 'results'):
        for index, source in enumerate(data.get(key, [])):
            if contains_secret(source, secrets):
                data[key][index] = {'error': {'code': 'fetch_failed'}}
    if contains_secret(data, secrets):
        return {'error': {'code': 'fetch_failed'}}
    return data


# From src/kagi_crawler/budget.py at c7f1cbca.


def result(data, *, error=False):
    # Compact JSON reduces duplication overhead. The budget counts the escaped
    # text copy AND structuredContent, wrappers and isError, but not transport
    # JSON-RPC id/envelope, HTTP headers or client-specific tokenizer overhead.
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(
        data, ensure_ascii=False, separators=(",", ":")))],
        structuredContent=data, isError=error)


def wire_bytes(data):
    return len(result(data).model_dump_json(exclude_none=True).encode("utf-8"))


def apply_budget(data, limit):
    """Return valid bounded JSON with explicit source/content coverage.

    `limit` is validated by the public contract. Top-level semantic metadata is
    preserved; if it alone cannot fit, return an explicit budget error instead
    of silently stripping fields. No token equivalence or Exa parity is implied.
    """
    output = deepcopy(data)
    key = 'sources' if 'sources' in output else 'results'
    sources = output.get(key, [])
    original_count = len(sources)
    indices = list(range(original_count))
    coverage = output['budget'] = {
        'limit_bytes': limit, 'unit': 'serialized_mcp_result_utf8_bytes',
        'source_count': original_count, 'returned_source_count': original_count,
        'failed_source_count': sum('error' in s for s in sources),
        'omitted_source_indices': [], 'partial_coverage': any('error' in s or s.get('pdf', {}).get('partial') or s.get('coverage', {}).get('partial') for s in sources)
            or bool(output.get('selection', {}).get('partial_coverage'))
            or bool(output.get('traversal', {}).get('omissions')),
    }
    if wire_bytes(output) <= limit:
        return output

    # Fairly reduce large text across all sources before omitting any records.
    # Repeated halving avoids letting the first page monopolize the response.
    while wire_bytes(output) > limit:
        candidates = [(s, field) for s in sources
                      for field in ('content', 'provider_snippet', 'title')
                      if isinstance(s.get(field), str) and len(s[field]) > 64]
        if not candidates:
            break
        coverage['partial_coverage'] = True
        for source, field in candidates:
            source[field] = source[field][:max(64, len(source[field]) // 2)]
            truncated = source.setdefault('truncated_fields', [])
            if field not in truncated:
                truncated.append(field)

    # Expansion context is optional, but always a complete canonical section.
    # Omit it before the original quote; never truncate offsets or references.
    while wire_bytes(output) > limit:
        candidates = [s for s in sources if s.get('context')]
        if not candidates:
            break
        coverage['partial_coverage'] = True
        for source in candidates:
            source['context'].pop()
            source['omitted_context_section_count'] = source.get('omitted_context_section_count', 0) + 1

    # Highlights are atomic: .6 owns relevance/qualification-preserving ordering.
    while wire_bytes(output) > limit:
        candidates = [s for s in sources if s.get('content_mode') != 'evidence_expansion'
                      and isinstance(s.get('highlights'), list) and s['highlights']]
        if not candidates:
            break
        coverage['partial_coverage'] = True
        for source in candidates:
            source['highlights'].pop()
            source['omitted_highlight_count'] = source.get('omitted_highlight_count', 0) + 1

    # Never chop attribution/error/version metadata to manufacture a fit. Retain
    # small failures and successes where possible by dropping the largest record.
    while sources and wire_bytes(output) > limit:
        position = max(range(len(sources)), key=lambda i: len(json.dumps(sources[i]).encode()))
        sources.pop(position)
        coverage['omitted_source_indices'].append(indices.pop(position))
        coverage['omitted_source_indices'].sort()
        coverage['returned_source_count'] = len(sources)
        coverage['partial_coverage'] = True
    if wire_bytes(output) > limit:
        return {'error': {'code': 'response_budget_exceeded', 'message':
                'Response metadata cannot fit the requested budget.'},
                'budget': {'limit_bytes': limit, 'source_count': original_count,
                           'returned_source_count': 0, 'partial_coverage': True}}
    return output
