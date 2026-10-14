"""Shared output contracts; plain dictionaries preserve the existing JSON wire.

Inputs stay in contract.py. Keeping outputs here avoids dependencies on Pydantic,
MCP transport or browser ownership when cache/traversal/evidence share records.
"""
from typing import Any, Literal, NotRequired, TypedDict


ErrorCode = Literal[
    'access_rejected', 'invalid_input', 'invalid_evidence_id', 'evidence_unavailable',
    'response_budget_exceeded', 'fetch_failed', 'fetch_timeout', 'queue_full',
    'queue_timeout', 'policy_rejected', 'scope_rejected', 'traversal_byte_limit',
    'cache_miss', 'browser_unavailable', 'rate_limited', 'blocked', 'resource_limit',
    'unsupported_format', 'pdf_parse_failed', 'upstream_authentication',
    'discovery_failed', 'discovery_timeout', 'discovery_output_limit',
    'discovery_queue_full', 'discovery_queue_timeout', 'discovery_cli_version',
    'discovery_parse_error',
]


class ErrorDetails(TypedDict, total=False):
    reason: str
    http_status: int


class SourceError(ErrorDetails):
    code: ErrorCode
    message: NotRequired[str]


class SiteAdapterCoverage(TypedDict):
    name: str
    outcome: Literal['applied', 'fallback']
    reason: NotRequired[Literal['public_preview']]


class Coverage(TypedDict):
    exhaustive: bool
    partial: bool
    actions: int
    stop_reason: str
    policy: str
    initial_render: NotRequired[dict[str, bool]]
    site_adapter: NotRequired[SiteAdapterCoverage]


class PDFReference(TypedDict):
    page: int
    start: int
    end: int
    status: str


class PDFMetadata(TypedDict):
    total_pages: int
    processed_pages: int
    omitted_pages: int
    unsupported_pages: list[int]
    partial: bool
    references: list[PDFReference]
    parser: str
    layout: str


class SourceSuccess(TypedDict):
    requested_url: str
    resolved_url: str
    title: str
    document_type: Literal['html', 'pdf']
    fetched_at: str
    cache_state: NotRequired[str]
    content_version: str
    content_mode: str
    content: str
    stale: NotRequired[bool]
    refresh_error: NotRequired[SourceError]
    pdf: NotRequired[PDFMetadata]
    coverage: NotRequired[Coverage]
    # Private acquisition/cache metadata, removed before MCP serialization.
    link_targets: NotRequired[list[str]]
    links_truncated: NotRequired[bool]


class SourceFailure(TypedDict):
    error: SourceError
    requested_url: NotRequired[str | None]
    evidence_id: NotRequired[str]


Source = SourceSuccess | SourceFailure


class TraversalMetadata(TypedDict):
    scope: dict[str, str]
    limits: dict[str, int | float]
    attempted_pages: int
    accounted_bytes: int
    omissions: dict[str, int]
    exhaustive: bool
    network_scope_enforced: bool


class AcquisitionResult(TypedDict):
    sources: list[Source]
    content_mode: str
    traversal: NotRequired[TraversalMetadata]


class BrowserHTML(TypedDict):
    url: str
    title: str
    content: str
    links: list[str]
    links_truncated: bool
    coverage: Coverage


class BrowserPDF(TypedDict):
    url: str
    pdf: bytes


BrowserResult = BrowserHTML | BrowserPDF

# Evidence selection, discovery enrichment and budgeting project heterogeneous
# source records (removing content and adding references/counters). Their JSON
# metadata is intentionally extensible; canonical cache records above are not.
ToolData = dict[str, Any]


class ToolResult(TypedDict, total=False):
    """Public envelope after discovery/evidence projection and byte budgeting."""
    sources: list[ToolData]
    results: list[ToolData]
    content_mode: str
    error: SourceError
    traversal: TraversalMetadata
    budget: ToolData
    selection: ToolData
    expansion: ToolData
    acquisition_limits: ToolData
    postfilters: list[str]
    discovery_limits: ToolData
    date_semantics: str
    warnings: list[str]
