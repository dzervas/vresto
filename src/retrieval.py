"""Search/fetch/expand/crawl orchestration returning final public, budgeted tool data."""
from copy import deepcopy
import asyncio
import logging

from .acquisition import Acquisition
from .budget import DEFAULT_RESPONSE_BYTES, apply_budget
from .cache import CachedAcquisition, DocumentCache
from .contract import CrawlInput, ExpandInput, FetchInput, SearchInput, FetchRequest, SearchRequest
from .discovery import Discovery, DiscoveryError
from .evidence import expand_evidence, select_highlights
from .memory import Memory
from .safety import safe_sources
from .summaries import add_summaries
from .traversal import Traversal
from .types import ToolData
from .deadline import request_deadline


def _log_failures(data: ToolData) -> None:
    # One line per failed or stale-fallback source, with its URL, so blocked
    # sites can be found in the logs. Runs after safe_sources, which already
    # replaced any credential-bearing source with a URL-less failure.
    logger = logging.getLogger('vresto')
    for source in data.get('sources', data.get('results', [])):
        url = source.get('requested_url') or source.get('resolved_url') or source.get('url') or '-'
        diagnostic = source.pop('_diagnostic', {})
        error = source.get('error')
        if error is not None:
            logger.warning('source_failed url=%s code=%s reason=%s http_status=%s stage=%s', url,
                           error.get('code'), error.get('reason', diagnostic.get('reason', '-')),
                           error.get('http_status', diagnostic.get('http_status', '-')), diagnostic.get('stage', '-'))
        elif (refresh := source.get('refresh_error')) is not None:
            logger.warning('source_stale url=%s code=%s reason=%s http_status=%s stage=%s', url,
                           refresh.get('code'), refresh.get('reason', diagnostic.get('reason', '-')),
                           refresh.get('http_status', diagnostic.get('http_status', '-')), diagnostic.get('stage', '-'))


class Retrieval:
    def __init__(self, discovery: Discovery, acquisition: Acquisition,
                 cached_acquisition: CachedAcquisition, cache: DocumentCache,
                 secrets: tuple[str, ...], response_budget_bytes: int = DEFAULT_RESPONSE_BYTES,
                 memory: Memory | None = None) -> None:
        self.response_budget_bytes = response_budget_bytes
        self.discovery = discovery
        self.acquisition = acquisition
        self.cached_acquisition = cached_acquisition
        self.cache = cache
        self.secrets = secrets
        self.memory = memory

    def _acquisition_limits(self, page_limit):
        options = self.acquisition.options
        return {'returned_page_limit': page_limit, 'request_deadline_seconds': options.deadline,
                'rendered_dom_bytes': options.dom_bytes,
                'post_acquisition_document_bytes': options.response_bytes,
                'expansion_actions': options.expansion_actions,
                'expansion_seconds': options.expansion_timeout,
                'native_network_resource_bounds': 'operator_memory_disk_egress_limits',
                'browser_concurrency': options.browser_concurrency}

    def _finalize(self, data: ToolData, request: SearchRequest | FetchRequest,
                  retained: list[ToolData] | None = None) -> ToolData:
        # The only output boundary. Strip private traversal metadata from a
        # copy, preserving canonical/cache records. Reject credentials before
        # selection so contaminated quotations cannot influence admission.
        data = deepcopy(data)
        for source in data.get('sources', data.get('results', [])):
            source.pop('link_targets', None)
            source.pop('links_truncated', None)
        data = safe_sources(data, self.secrets)
        _log_failures(data)
        highlights = ((request.content_mode == 'summary' and request.query is not None) or
                      request.content_mode == 'highlights' or
                      (isinstance(request, FetchRequest) and request.content_mode is None and request.query))
        if highlights and 'error' not in data:
            assert request.query is not None  # Validated highlight mode requires a query.
            data = select_highlights(data, request.query, request.response_budget_bytes)
            # Selection adds normalized query terms; those can newly match a
            # credential even when the original input's case did not.
            data = safe_sources(data, self.secrets)
        if request.content_mode == 'summary' and 'error' not in data:
            data['content_mode'] = 'summary'
            for source in data.get('sources', data.get('results', [])):
                source.pop('content', None)
                if 'error' not in source:
                    source['content_mode'] = 'summary'
        if retained:
            # Already ranked exact passages, not input to lexical highlighting or
            # generated summaries. Live sources have first claim on the budget.
            data['results'].extend(safe_sources({'results': retained}, self.secrets)['results'])
        return apply_budget(data, request.response_budget_bytes)

    async def fetch(self, request: FetchInput | FetchRequest) -> ToolData:
        with request_deadline(self.acquisition.options.deadline) as deadline:
            return await self._fetch(request, deadline)

    async def _fetch(self, request: FetchInput | FetchRequest, deadline: float) -> ToolData:
        if isinstance(request, FetchInput):
            request = FetchRequest(**request.model_dump(), response_budget_bytes=self.response_budget_bytes)
        data: ToolData
        if request.evidence_ids is not None:
            try:
                async with asyncio.timeout_at(deadline):
                    data = dict(await asyncio.to_thread(expand_evidence, self.cache, request.evidence_ids,
                                                       self.memory.store if self.memory is not None else None))
            except TimeoutError:
                return {'error': {'code': 'fetch_timeout'}}
            except ValueError:
                # Evidence validation also checks retained versions/offsets;
                # never forward the offending ID or exception details.
                return {"error": {"code": "invalid_input", "message":
                    "Invalid tool input or unsupported control; see tool input schema."}}
        else:
            assert request.urls is not None  # Validated exclusive URL/evidence modes.
            with self.acquisition.request():
                if request.traversal is not None:
                    data = dict(await Traversal(self.cached_acquisition, request, self.acquisition.options).run(request.urls[0]))
                else:
                    data = dict(await self.cached_acquisition.fetch_many(request.urls, freshness=request.freshness,
                                                               max_age_seconds=request.max_age_seconds))
            data['acquisition_limits'] = self._acquisition_limits(request.traversal.max_pages if request.traversal else 10)
        if request.content_mode == 'summary':
            data = safe_sources(data, self.secrets)
            await add_summaries(data, self.discovery, self.cache, self.secrets, 'digest', deadline)
        return self._finalize(data, request)

    async def search(self, request: SearchInput | SearchRequest) -> ToolData:
        with request_deadline(self.acquisition.options.deadline) as deadline:
            return await self._search(request, deadline)

    async def _search(self, request: SearchInput | SearchRequest, deadline: float) -> ToolData:
        if isinstance(request, SearchInput):
            request = SearchRequest(**request.model_dump(), response_budget_bytes=self.response_budget_bytes)
        data: ToolData
        try:
            data = dict(await self.discovery.search(request))
            if request.content_mode != 'discovery':
                # One request-wide deadline across all returned pages, including
                # cache refresh loops; each URL must not reset the shared deadline.
                with self.acquisition.request():
                    acquired = await self.cached_acquisition.fetch_many([r['url'] for r in data['results']],
                        freshness=request.freshness, max_age_seconds=request.max_age_seconds)
                data['results'] = [dict(result, **source) for result, source in
                                   zip(data['results'], acquired['sources'])]
                data['content_mode'] = 'full_text'
                data['acquisition_limits'] = self._acquisition_limits(20)
            if request.content_mode == 'summary':
                data = safe_sources(data, self.secrets)
                await add_summaries(data, self.discovery, self.cache, self.secrets, 'overview', deadline)
            retained = []
            if request.content_mode != 'discovery' and self.memory is not None and request.freshness != 'fresh_only':
                exclude = {url for result in data['results'] for key in ('url', 'requested_url', 'resolved_url')
                           if (url := result.get(key))}
                retained = await self.memory.query(request.query, request.num_results, exclude,
                    timeout_seconds=min(5, max(0, deadline - asyncio.get_running_loop().time())))
            return self._finalize(data, request, retained)
        except DiscoveryError as error:
            messages = {
                "upstream_authentication": "Kagi subscriber session missing, rejected or expired.",
                "rate_limited": "Kagi rate limited discovery; retry later.",
                "discovery_parse_error": "Invalid discovery output; upstream HTML/parser may have changed.",
                "discovery_failed": "Kagi discovery failed; no paid API fallback was attempted.",
                "discovery_timeout": "Kagi discovery exceeded the service deadline.",
                "discovery_output_limit": "Kagi CLI output exceeded the service byte limit.",
                "discovery_cli_version": "Install the pinned Microck Kagi CLI revision; release mismatch.",
            }
            return {"error": {"code": error.code, "message": messages[error.code]}}

    async def expand(self, request: ExpandInput) -> ToolData:
        return await self.fetch(FetchRequest(evidence_ids=request.evidence_ids,
                                            response_budget_bytes=self.response_budget_bytes))

    async def crawl(self, request: CrawlInput) -> ToolData:
        return await self.fetch(FetchRequest(urls=[request.url], traversal=request.traversal(),
                                            response_budget_bytes=self.response_budget_bytes))
