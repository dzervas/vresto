"""Only implemented discovery controls are part of the public contract."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=4096)
    # Exa comparison: exa-labs/exa-mcp-server e9c3b0126a3373eb1aeaca162e84d0791ff4c7f7,
    # src/tools/webSearch.ts: query + numResults, default 10, contents.highlights=true;
    # text output labels Title/URL/Published/Author/Highlights. Here snake_case inputs
    # and structured source records deliberately distinguish provider_snippet from
    # acquired evidence. No Exa category/type, generated highlights, or date claims.
    # Temporary discovery-only default: later evidence tickets add highlights/fetch.
    num_results: int = Field(default=10, ge=1, le=20)
    content_mode: Literal["discovery"] = "discovery"
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
