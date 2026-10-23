"""One byte budget for the complete MCP CallToolResult, not a tokenizer claim.

Postprocessing contract: pass a JSON-compatible response with `sources` or
`results` in priority order AFTER evidence selection, credential redaction and
cache writes. This function copies it; canonical documents must never be trimmed.
Only content/title/provider_snippet and labeled generated text may be
prefix-truncated. Other metadata (including URLs, versions, error codes and evidence references)
remains intact or its entire source is omitted with its original zero-based index.
Highlights are complete passage records: remove whole records, never shorten
quotes or detach qualifications/references. Evidence selection fits coherent
passages first; this is the final wire-size safety gate.
"""
from typing import cast
from copy import deepcopy
import json

from mcp.types import CallToolResult

from .types import ToolData, ToolResult

# 4 KiB retains attributed fixture content; 256 KiB fits the multilingual fixture.
# Frozen eval corpus at 12,288 bytes (compact wire / indent-2 structuredContent):
# c05 highlights = 12,163/14,416 bytes; c06 = 12,277/14,477; c08 full_text
# = 11,779/14,266 (pretty/compact <= 1.22 for these near-limit responses).
# Python indent=2 with ASCII escaping is conservative versus pi JSON.stringify;
# 12 KiB leaves headroom below pi's 20 KB model-text cap, not a universal pretty
# size guarantee: metadata-heavy results can have higher ratios. Operators can
# override the compact-wire budget; this is not a token or production sizing claim.
MIN_RESPONSE_BYTES = 4096
DEFAULT_RESPONSE_BYTES = 12288
MAX_RESPONSE_BYTES = 262144


def result(data: ToolData | ToolResult, *, error: bool = False) -> CallToolResult:
    # Count one serialized structuredContent copy plus the CallToolResult fields
    # content=[] and isError; exclude the JSON-RPC envelope and HTTP headers.
    # This is compact UTF-8 wire JSON, not tokens or model-visible text: pi
    # pretty-prints structuredContent with 2-space indentation, which is larger.
    # MCP 1.26's lowlevel Server passes explicit CallToolResult through unchanged,
    # before dict-to-text mirroring and output-schema validation.
    return CallToolResult(content=[], structuredContent=cast(ToolData, data), isError=error)


def wire_bytes(data: ToolData | ToolResult) -> int:
    # Match the server's top-level error flag as well as its serialization.
    return len(result(data, error='error' in data).model_dump_json(
        by_alias=True, exclude_none=True).encode("utf-8"))


def apply_budget(data: ToolData, limit: int) -> ToolData:
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
            or bool(output.get('traversal', {}).get('omissions'))
            or 'quick_answer_error' in output,
    }
    if wire_bytes(output) <= limit:
        return output

    # Quick Answer is generated orientation, not evidence. Shed its payload
    # before changing discovery links or acquired sources; never trim the
    # canonical cache or silently shorten a quotation to make generation fit.
    quick = output.get('quick_answer', {})
    while len(quick.get('text', '')) > 64 and wire_bytes(output) > limit:
        quick['text'] = quick['text'][:max(64, len(quick['text']) // 2)]
        quick['response_truncated'] = True
        coverage['partial_coverage'] = True
    references = quick.get('references', [])
    while references and wire_bytes(output) > limit:
        removed = max(1, len(references) // 2)
        del references[-removed:]
        quick['omitted_reference_count'] = quick.get('omitted_reference_count', 0) + removed
        coverage['partial_coverage'] = True

    # Search memory is supplementary: omit its lowest-ranked results before
    # changing live data. Explicit expand requests still shed context first.
    while key == 'results' and wire_bytes(output) > limit:
        positions = [i for i, source in enumerate(sources) if source.get('origin') == 'memory']
        if not positions:
            break
        position = positions[-1]
        sources.pop(position)
        coverage['omitted_source_indices'].append(indices.pop(position))
        coverage['omitted_source_indices'].sort()
        coverage['returned_source_count'] = len(sources)
        coverage['partial_coverage'] = True

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

    # Highlight selection may retain unselected outcomes internally to preserve
    # original source indices. Prefer their metadata omissions over losing an
    # admitted exact quote or duplicate attribution. Full text/expansion keep
    # their existing reduction order.
    while output.get('content_mode') == 'highlights' and wire_bytes(output) > limit:
        candidates = [i for i, s in enumerate(sources)
                      if not s.get('highlights') and not s.get('duplicate_evidence')]
        if not candidates:
            break
        position = max(candidates, key=lambda i: len(json.dumps(sources[i]).encode()))
        sources.pop(position)
        coverage['omitted_source_indices'].append(indices.pop(position))
        coverage['omitted_source_indices'].sort()
        coverage['returned_source_count'] = len(sources)
        coverage['partial_coverage'] = True

    # Highlights are atomic: evidence selection owns relevance/qualification ordering.
    while wire_bytes(output) > limit:
        candidates = [s for s in sources if s.get('content_mode') != 'evidence_expansion'
                      and isinstance(s.get('highlights'), list) and s['highlights']]
        if not candidates:
            break
        coverage['partial_coverage'] = True
        for source in candidates:
            source['highlights'].pop()
            source['omitted_highlight_count'] = source.get('omitted_highlight_count', 0) + 1

    # Generated prose is preferred to quotes in summary mode. Only after
    # atomic highlights are gone may summaries be shortened, with disclosure.
    while wire_bytes(output) > limit:
        candidates = [s for s in sources if isinstance(s.get('summary'), dict)
                      and len(s['summary'].get('text', '')) > 64]
        if not candidates:
            break
        coverage['partial_coverage'] = True
        for source in candidates:
            summary = source['summary']
            summary['text'] = summary['text'][:max(64, len(summary['text']) // 2)]
            summary['response_truncated'] = True

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
