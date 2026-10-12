"""Validated discovery, acquisition and query-focused content controls."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .budget import DEFAULT_RESPONSE_BYTES, MIN_RESPONSE_BYTES, MAX_RESPONSE_BYTES


class BudgetInput(BaseModel):
    response_budget_bytes: int = Field(default=DEFAULT_RESPONSE_BYTES,
        ge=MIN_RESPONSE_BYTES, le=MAX_RESPONSE_BYTES,
        description="Shared UTF-8 byte limit for the serialized MCP tool result, including "
                    "text and structuredContent copies and metadata; not a token count. "
                    "Transport JSON-RPC envelope excluded. Truncation/omissions are explicit.")


class FetchInput(BudgetInput):
    model_config = ConfigDict(extra="forbid", strict=True)
    urls: list[str] = Field(min_length=1, max_length=10)
    content_mode: Literal["full_text", "highlights"] | None = Field(default=None,
        description="Defaults to highlights when query is supplied, otherwise full_text.")
    query: str | None = Field(default=None, min_length=1, max_length=4096)
    freshness: Literal['ordinary', 'refresh', 'fresh_only', 'cache_only'] = 'ordinary'
    max_age_seconds: float = Field(default=3600, ge=0, le=2_592_000)

    @model_validator(mode='after')
    def validate_relevance(self):
        if self.query is not None and not self.query.strip():
            raise ValueError('empty query')
        if self.content_mode == 'highlights' and self.query is None:
            raise ValueError('highlights require query')
        return self


class SearchInput(BudgetInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=4096)
    # Exa comparison: exa-labs/exa-mcp-server e9c3b0126a3373eb1aeaca162e84d0791ff4c7f7,
    # src/tools/webSearch.ts: query + numResults, default 10, contents.highlights=true;
    # text output labels Title/URL/Published/Author/Highlights. Here snake_case inputs
    # and structured source records deliberately distinguish provider_snippet from
    # acquired evidence. No Exa category/type, generated highlights, or date claims.
    # This source-shape comparison is not a live Exa response/budget comparison:
    # no authorized Exa gateway was available; quality/parity remains unverified.
    num_results: int = Field(default=10, ge=1, le=20)
    content_mode: Literal["discovery", "highlights", "full_text"] = "highlights"
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
