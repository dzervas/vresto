"""One byte budget for the complete MCP CallToolResult, not a tokenizer claim.

Postprocessing contract: pass a JSON-compatible response with `sources` or
`results` in priority order AFTER evidence selection, credential redaction and
cache writes. This function copies it; canonical documents must never be trimmed.
Only content/title/provider_snippet strings may be prefix-truncated. Other
metadata (including URLs, versions, error codes and future evidence references)
remains intact or its entire source is omitted with its original zero-based index.
Future highlights must be a list of complete passage records: remove whole
records, never shorten quotes or detach qualifications/references. Selection in
.6 should fit coherent evidence first; this is the final wire-size safety gate.
"""
from copy import deepcopy
import json

from mcp.types import CallToolResult, TextContent

# Provisional fixture measurements through real HTTP MCP + native Chromium
# (tests/test_budget.py, 2026-10-12), including duplicate serialization:
# ten discovery records: 3,086 bytes; nine multilingual technical documents
# (300 repeated qualified sentences each) + one failure: 183,149 bytes intact.
# 64 KiB returned all ten outcomes in 51,405 bytes with explicit text truncation;
# 16 KiB retained ten in 12,957 bytes; 4 KiB retained four in 3,513 bytes with
# six explicit omissions. Thus 4 KiB allows meaningful attribution/content,
# 64 KiB is a moderate shared-context default and 256 KiB accommodates this
# intact larger fixture while bounding output. Not production sizing or Exa
# parity; client tokenization and deployment workloads still need measurement.
MIN_RESPONSE_BYTES = 4096
DEFAULT_RESPONSE_BYTES = 65536
MAX_RESPONSE_BYTES = 262144


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
        'omitted_source_indices': [], 'partial_coverage': any('error' in s for s in sources),
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

    # Highlights are atomic: .6 owns relevance/qualification-preserving ordering.
    while wire_bytes(output) > limit:
        candidates = [s for s in sources if isinstance(s.get('highlights'), list) and s['highlights']]
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
