"""Contain fragile site markup here, never in the generic pipeline.

To fix a site, edit only its adapter and tests/sites/<site>/ fixtures/tests.
To add a site, also append its adapter to ADAPTERS below: the explicit order
is precedence (first URL match wins). There is no discovery/plugin machinery.
Adapters have a name, URL matcher, and optional expansion/extraction hooks.
Expansion supplies selectors/JS to the existing bounded read-only loop; it
may optionally authorize controls with authorize_selector (passed as an evaluate
argument, never interpolated JS). The generic loop still requires visibility,
control/target scope, structural aria-controls association, no forms/nav/dialogs,
no submit controls, and anchors with only empty/fragment hrefs; size, action,
duration, deadline and document revalidation guards apply to every action. Extraction
returns Extracted, ExtractionOutcome, or None to decline. Outcome blocked raises
FetchError('blocked', reason='login_required'); unavailable raises
FetchError('fetch_failed', reason='content_unavailable'). These failures use the
ordinary cache/stale rules, never cache wall text. Partial wraps Extracted and
sets coverage.partial with site_adapter.reason='public_preview'. Generic
normalization/conversion is the default. Any Exception from a matcher/hook falls back; BaseException (including
cancellation) propagates. Diagnostics contain only fixed phases/reasons and
exception class names, never messages, HTML or URLs.

Coverage omits site_adapter for generic pages. Matched adapters disclose
{name, outcome: 'applied'|'fallback'}; an absent hook does not mean failure.
Fallback in either phase wins over success in the other; classified failures
always propagate, even after expansion fallback. No custom extraction
hook means generic extraction.

Required adapter header template:
    Targeted page types: ...
    Markup assumptions (verified YYYY-MM-DD): ...
    Known fragility: ...
    Fixtures: tests/sites/<site>/... (HTML and golden Markdown)
    Refresh: uv run python scripts/capture_fixture.py <url> <fixture-path>
Check the capture tool's --help for its current flags. Never capture private
pages/credentials. Add regression fixtures before changing fragile selectors.
"""
from collections.abc import Awaitable, Callable
import logging
from typing import Literal
from urllib.parse import urlsplit

from patchright.async_api import Page

from ..acquisition_policy import AcquisitionOptions, FetchError
from ..types import Coverage

from ..extraction import Extracted, extract
from ..content_expansion import expand_content as expand_generic
from .base import Expansion, ExtractionOutcome, SiteAdapter
from .github import ADAPTER as GITHUB
from .reddit import ADAPTER as REDDIT
from .stackoverflow import ADAPTER as STACKOVERFLOW
from .x import ADAPTER as X

# Explicit precedence; no discovery.
# Host matchers are disjoint today, so order only matters for future overlaps.
ADAPTERS: tuple[SiteAdapter, ...] = (GITHUB, STACKOVERFLOW, REDDIT, X)


def _diagnostic(error: Exception) -> None:
    # Same privacy contract as browser._diagnostic: no exception text or URL.
    try:
        logging.getLogger('kagi_crawler').warning(
            'site_adapter_failure stage=site_adapter type=%s reason=adapter_exception',
            type(error).__name__)
    except Exception:
        # Diagnostics must never replace the generic fallback.
        pass


def _disclose(coverage: Coverage | None, adapter: SiteAdapter,
              outcome: Literal['applied', 'fallback']) -> None:
    if coverage is not None:
        previous = coverage.get('site_adapter')
        if previous and previous['name'] == adapter.name and previous['outcome'] == 'fallback':
            outcome = 'fallback'
        coverage['site_adapter'] = {'name': adapter.name, 'outcome': outcome}


def select(url: str, coverage: Coverage | None = None) -> SiteAdapter | None:
    parsed = urlsplit(url)
    for adapter in ADAPTERS:
        try:
            if adapter.matches(parsed):
                return adapter
        except Exception as error:
            _diagnostic(error)
            _disclose(coverage, adapter, 'fallback')
            return None
    return None


def extract_document(document: str, url: str, *, coverage: Coverage | None = None) -> Extracted:
    adapter = select(url, coverage)
    previous = coverage.get('site_adapter') if coverage is not None else None
    if adapter is not None and adapter.extract is not None:
        try:
            result = adapter.extract(document, url)
        except Exception as error:
            _diagnostic(error)
            result = None
        # Interpret outside the fail-soft hook boundary: classified failures
        # must never become generic success or enter the content cache, even
        # when the expansion phase fell back.
        if isinstance(result, ExtractionOutcome):
            if result.status == 'blocked':
                raise FetchError('blocked', reason='login_required')
            if result.status == 'unavailable':
                raise FetchError('fetch_failed', reason='content_unavailable')
            if result.content is not None:
                if previous and previous['name'] == adapter.name and previous['outcome'] == 'fallback':
                    return extract(document, url)
                _disclose(coverage, adapter, 'applied')
                if coverage is not None:
                    coverage['partial'] = True
                    disclosure = coverage.get('site_adapter')
                    if disclosure is not None:
                        disclosure['reason'] = 'public_preview'
                return result.content
        elif result is not None:
            if previous and previous['name'] == adapter.name and previous['outcome'] == 'fallback':
                return extract(document, url)
            _disclose(coverage, adapter, 'applied')
            return result
        _disclose(coverage, adapter, 'fallback')
    return extract(document, url)


async def expand_content(page: Page, options: AcquisitionOptions, *, deadline: float,
                         revalidate: Callable[[], Awaitable[bool]]) -> Coverage:
    # Resolve the specialization before actions, so a failed hook cannot cause
    # generic retries that spend the action budget twice.
    disclosure: Coverage = {'exhaustive': False, 'partial': False, 'actions': 0,
                            'policy': 'bounded_read_only_in_page_controls', 'stop_reason': 'settled'}
    adapter = select(page.url, disclosure)
    expansion = None
    if adapter is not None and adapter.expansion is not None:
        try:
            expansion = adapter.expansion()
        except Exception as error:
            _diagnostic(error)
        _disclose(disclosure, adapter, 'applied' if expansion is not None else 'fallback')
    def failed(error: Exception) -> None:
        _diagnostic(error)
        if adapter is not None:
            _disclose(disclosure, adapter, 'fallback')

    expansion = expansion or Expansion()
    coverage = await expand_generic(page, options, deadline=deadline, revalidate=revalidate,
                                    scope_selector=expansion.scope_selector,
                                    deduplicate=expansion.deduplicate,
                                    authorize_selector=expansion.authorize_selector, on_failure=failed)
    if 'site_adapter' in disclosure:
        coverage['site_adapter'] = disclosure['site_adapter']
    return coverage

