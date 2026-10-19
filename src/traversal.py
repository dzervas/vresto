"""Bounded breadth-first traversal over cached acquisition.

Bounds attempts (including seed, failures and cache hits), depth, frontier,
wall time and canonical bytes. Pages are visited one at a time; no parallel
browser work is created here, preserving attempt/byte bounds and BFS order.
Exa-style related-document ergonomics, not discovery/index parity; no authorized live Exa comparison was available.
"""
from typing import TYPE_CHECKING
import asyncio
from collections import Counter
import posixpath
from urllib.parse import urlsplit, unquote, urljoin

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import AcquisitionResult, Source

if TYPE_CHECKING:
    from .cache import CachedAcquisition
    from .contract import FetchRequest

from .acquisition_policy import AcquisitionOptions, DestinationPolicy, FetchError
from .deadline import remaining


class TraversalInput(BaseModel):
    # Internal containment bounds; crawl exposes only max_pages and path scope.
    # each page also inherits service queue/duration/document/parser limits. These
    # are not measured deployment sizing or inferred Exa coverage guarantees.
    model_config = ConfigDict(extra='forbid', strict=True)
    origin: str = Field(max_length=4096, description='Explicit exact HTTP(S) origin, including port; no subdomains.')
    path_prefix: str = Field(max_length=4096, description='Explicit path subtree; / opts into the whole exact origin.')
    max_pages: int = Field(default=5, ge=1, le=20, description='Attempted pages including seed, failures and cache hits.')
    max_depth: int = Field(default=1, ge=0, le=3)
    max_seconds: float | None = Field(default=None, gt=0, le=30,
        description='Optional internal cap; public crawls use the shared request deadline.')
    max_frontier: int = Field(default=32, ge=1, le=128)
    max_bytes: int = Field(default=8_388_608, ge=1024, le=33_554_432,
        description='Canonical document and link bytes (including cache reuse); post-acquisition, not native traffic or response budget.')

    @model_validator(mode='after')
    def scope_shape(self):
        p = urlsplit(self.origin)
        if (p.scheme not in ('http', 'https') or not p.hostname
            or p.username is not None or p.password is not None
            or p.path not in ('', '/') or p.query or p.fragment
            or not self.path_prefix.startswith('/') or self.path_prefix.startswith('//')
            or '?' in self.path_prefix or '#' in self.path_prefix):
            raise ValueError('origin and path subtree required')
        p.port  # Validate port syntax before acquisition.
        try:
            path(self.path_prefix)
        except FetchError:
            raise ValueError('invalid path subtree') from None
        return self


def origin(url: str) -> tuple[str, str | None, int]:
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)


def path(url: str) -> str:
    value = urlsplit(url).path
    # Conservative subtree interpretation across servers with encoded slashes or
    # dot segments: repeatedly decode before comparing, never widen the scope.
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    if '%' in value or '\\' in value:
        raise FetchError('scope_rejected')
    return posixpath.normpath('/' + value.lstrip('/'))


class Traversal:
    def __init__(self, acquisition: "CachedAcquisition", request: "FetchRequest", options: AcquisitionOptions) -> None:
        self.acquisition = acquisition
        self.options = options
        assert request.traversal is not None  # Only constructed for traversal requests.
        self.input = request.traversal
        self.request = request
        self.policy = DestinationPolicy(options)
        self.scope_origin = origin(self.input.origin)
        self.prefix = path(self.input.path_prefix).rstrip('/')
        self.sources: list[Source] = []
        self.seen = set()
        self.attempted = 0
        self.omissions = Counter()
        self.bytes = 0
        self.stopped = False  # Finish the current level, then stop.
        self.current_url = None

    def check_document(self, url: str) -> str:
        url = self.policy.url(url)
        p = path(url)
        if (origin(url) != self.scope_origin
            or (self.prefix and p != self.prefix and not p.startswith(self.prefix + '/'))):
            raise FetchError('scope_rejected')
        return url

    def charge(self, size: int) -> None:
        if self.bytes + size > self.input.max_bytes:
            self.omissions['byte_limit'] += 1
            raise FetchError('traversal_byte_limit')
        self.bytes += size

    async def _visit(self, urls):
        """Acquire one BFS level in order; returns (url, source) per attempt."""
        visited = []
        for url in urls:
            if url in self.seen:
                continue
            if len(self.sources) >= self.input.max_pages:
                self.omissions['page_limit'] += 1
                self.stopped = True
                break
            self.seen.add(url)
            self.attempted += 1
            self.current_url = url
            source: Source
            try:
                self.check_document(url)
                source = (await self.acquisition.fetch_many([url], freshness=self.request.freshness,
                    max_age_seconds=self.request.max_age_seconds, scope=self.check_document))['sources'][0]
                if 'error' not in source:
                    self.check_document(source['resolved_url'])
                    self.seen.add(source['resolved_url'])
                    self.charge(len(source['content'].encode()) +
                        sum(len(link.encode()) for link in source.get('link_targets', [])))
            except FetchError as error:
                source = {'requested_url': url, 'error': {'code': error.code}}
            self.sources.append(source)
            self.current_url = None
            if source.get('error', {}).get('code') == 'fetch_timeout':
                self.omissions['time_limit'] += 1
                self.stopped = True
            if 'error' not in source and source.get('document_type') == 'html' and 'link_targets' not in source:
                self.omissions['link_metadata_unavailable'] += 1
            if source.get('error', {}).get('code') == 'scope_rejected':
                self.omissions['scope_rejected'] += 1
            if source.get('links_truncated'):
                self.omissions['link_limit'] += 1
            visited.append((url, source))
            if source.get('error', {}).get('code') == 'traversal_byte_limit':
                self.stopped = True
                break
        return visited

    def _discover(self, source, source_url, depth, queued, next_level):
        # Original anchor targets, queries included: ref/tracking parameters
        # can select different documents. Only fragments are disposable.
        queued.update(self.seen)
        for link in source.get('link_targets', []):
            try:
                url = self.check_document(urljoin(source_url, link))
            except FetchError:
                self.omissions['scope_rejected'] += 1
                continue
            if url in queued:
                continue
            if depth >= self.input.max_depth:
                self.omissions['depth_limit'] += 1
                continue
            if len(self.sources) + len(next_level) >= self.input.max_pages:
                self.omissions['page_limit'] += 1
                continue
            if len(next_level) >= self.input.max_frontier:
                self.omissions['frontier_limit'] += 1
                continue
            queued.add(url)
            next_level.append(url)

    async def _crawl(self, seed):
        level, depth, queued = [seed], 0, set()
        while level and not self.stopped:
            next_level = []
            for url, source in await self._visit(level):
                if 'error' not in source:
                    self._discover(source, url, depth, queued, next_level)
            level, depth = next_level, depth + 1

    async def run(self, seed: str) -> AcquisitionResult:
        try:
            self.scope_origin = origin(self.policy.url(self.input.origin))
            seed = self.check_document(seed)
            duration = remaining(self.options.deadline)
            if self.input.max_seconds is not None:
                duration = min(duration, self.input.max_seconds)
            async with asyncio.timeout(duration):
                await self._crawl(seed)
        except FetchError as error:
            self.sources.append({'requested_url': seed, 'error': {'code': error.code}})
        except TimeoutError:
            self.omissions['time_limit'] += 1
            if self.current_url is not None:
                self.sources.append({'requested_url': self.current_url, 'error': {'code': 'fetch_timeout'}})
        return {'sources': self.sources, 'content_mode': 'full_text', 'traversal': {
            'scope': {'origin': self.input.origin, 'path_prefix': self.input.path_prefix},
            'limits': self.input.model_dump(exclude={'origin', 'path_prefix'}),
            'attempted_pages': self.attempted, 'accounted_bytes': self.bytes,
            'omissions': dict(self.omissions), 'exhaustive': False,
            'network_scope_enforced': False}}
