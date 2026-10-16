"""Small adapter contracts shared by the registry and individual sites."""
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import SplitResult
from typing import Literal

from ..extraction import Extracted


@dataclass(frozen=True)
class ExtractionOutcome:
    """Classified failure, or a public preview with content for partial."""

    status: Literal['blocked', 'unavailable', 'partial']
    content: Extracted | None = None


@dataclass(frozen=True)
class Expansion:
    scope_selector: str = 'main, article, [role="main"]'
    deduplicate: str = '() => {}'
    authorize_selector: str | None = None


@dataclass(frozen=True)
class SiteAdapter:
    name: str
    matches: Callable[[SplitResult], bool]
    expansion: Callable[[], Expansion | None] | None = None
    extract: Callable[[str, str], Extracted | ExtractionOutcome | None] | None = None
