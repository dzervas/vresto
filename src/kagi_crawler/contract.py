"""Public MCP inputs and internal Retrieval controls (never expose Request schemas)."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .budget import DEFAULT_RESPONSE_BYTES, MIN_RESPONSE_BYTES, MAX_RESPONSE_BYTES
from .traversal import TraversalInput


class BudgetInput(BaseModel):
    response_budget_bytes: int = Field(default=DEFAULT_RESPONSE_BYTES,
        ge=MIN_RESPONSE_BYTES, le=MAX_RESPONSE_BYTES,
        description="Shared UTF-8 byte limit for the serialized MCP tool result, including "
                    "one structuredContent copy, content=[] and isError; not a token count. "
                    "Transport JSON-RPC envelope excluded. Truncation/omissions are explicit.")


class FetchRequest(BudgetInput):
    """Internal acquisition/expansion controls, not an MCP input schema."""
    model_config = ConfigDict(extra="forbid", strict=True)
    urls: list[str] | None = Field(default=None, min_length=1, max_length=10)
    evidence_ids: list[str] | None = Field(default=None, min_length=1, max_length=10,
        description="Expand exact retained passage versions, never fetch current pages. "
                    "Exclusive with URLs, query, content_mode, traversal and freshness controls; "
                    "only response_budget_bytes applies. Context sections may be omitted atomically.")
    traversal: TraversalInput | None = None
    content_mode: Literal["full_text", "highlights", "summary"] | None = Field(default=None,
        description="Defaults to highlights when query is supplied, otherwise full_text.")
    query: str | None = Field(default=None, min_length=1, max_length=4096)
    freshness: Literal['ordinary', 'refresh', 'fresh_only', 'cache_only'] = 'ordinary'
    max_age_seconds: float = Field(default=3600, ge=0, le=2_592_000)

    @model_validator(mode='after')
    def validate_relevance(self):
        if (self.urls is None) == (self.evidence_ids is None):
            raise ValueError('choose URLs or evidence IDs')
        if self.evidence_ids is not None and self.model_fields_set & {
                'urls', 'query', 'content_mode', 'freshness', 'max_age_seconds', 'traversal'}:
            raise ValueError('expansion excludes URL/relevance/freshness/traversal controls')
        if self.traversal is not None and (self.urls is None or len(self.urls) != 1):
            raise ValueError('traversal requires one URL seed')
        if self.query is not None and not self.query.strip():
            raise ValueError('empty query')
        if self.content_mode == 'highlights' and self.query is None:
            raise ValueError('highlights require query')
        return self


class SearchRequest(BudgetInput):
    """Internal discovery/retrieval controls, not an MCP input schema."""
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=4096)
    # Structured sources distinguish provider snippets from acquired evidence;
    # no generated highlights or inferred publication dates/provider parity.
    num_results: int = Field(default=10, ge=1, le=20)
    content_mode: Literal["discovery", "highlights", "full_text", "summary"] = "highlights"
    freshness: Literal['ordinary', 'refresh', 'fresh_only', 'cache_only'] = 'ordinary'
    max_age_seconds: float = Field(default=3600, ge=0, le=2_592_000)
    region: str | None = Field(default=None, pattern=r"^(?:[a-z]{2}|no_region)$")
    lens: int | None = Field(default=None, ge=0, le=4294967295)
    time: Literal["day", "week", "month", "year"] | None = None
    from_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    order: Literal["default", "recency", "website", "trackers"] | None = None
    verbatim: bool = False
    personalized: bool | None = None

    @model_validator(mode="after")
    def validate_controls(self):
        if not self.query.strip():
            raise ValueError("empty query")
        for value in (self.from_date, self.to_date):
            if value is not None:
                date.fromisoformat(value)
        if self.time and (self.from_date or self.to_date):
            raise ValueError("relative and explicit dates conflict")
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("reversed date range")
        return self


class SearchInput(BaseModel):
    """Model-facing search controls; operational controls stay server-side."""
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=4096)
    num_results: int = Field(default=10, ge=1, le=20)
    content_mode: Literal["discovery", "highlights", "full_text", "summary"] = "highlights"
    freshness: Literal["ordinary", "fresh_only"] = "ordinary"

    @model_validator(mode="after")
    def validate_query(self):
        if not self.query.strip():
            raise ValueError("empty query")
        return self


class FetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    urls: list[str] = Field(min_length=1, max_length=10)
    query: str | None = Field(default=None, min_length=1, max_length=4096)
    content_mode: Literal["highlights", "full_text", "summary"] | None = Field(default=None,
        description="Defaults to highlights with a query, otherwise full_text.")
    freshness: Literal["ordinary", "fresh_only"] = "ordinary"

    @model_validator(mode="after")
    def validate_query(self):
        if self.query is not None and not self.query.strip():
            raise ValueError("empty query")
        if self.content_mode == "highlights" and self.query is None:
            raise ValueError("highlights require query")
        return self


class ExpandInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)


class CrawlInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=4096)
    path_prefix: str = Field(max_length=4096,
        description="Path subtree to crawl; / selects the entire URL origin.")
    max_pages: int = Field(default=5, ge=1, le=20)

    def traversal(self) -> TraversalInput:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        return TraversalInput(origin=f"{parsed.scheme}://{parsed.netloc}",
                              path_prefix=self.path_prefix, max_pages=self.max_pages)

    @model_validator(mode="after")
    def validate_scope(self):
        self.traversal()
        return self
