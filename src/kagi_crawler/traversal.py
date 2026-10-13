"""Supported Crawl4AI BFS, with a serialized application acquisition facade.

Upstream counts successes, not attempts; the facade separately bounds attempts,
wall time and bytes (including cache reuse). No dispatcher or parallel browser
work is created here. Exa-style related-document ergonomics, not discovery/index
parity; no authorized live Exa comparison was available.
"""
import asyncio
from collections import Counter
from contextvars import ContextVar
import posixpath
from urllib.parse import urlsplit, unquote, urljoin

from crawl4ai import CrawlerRunConfig, CrawlResult
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .transport import FetchError


active_traversal = ContextVar('active_traversal', default=None)


class TraversalInput(BaseModel):
    # Provisional containment defaults/hard limits are published in MCP schema;
    # each page also inherits service queue/duration/document/parser limits. These
    # are not measured deployment sizing or inferred Exa coverage guarantees.
    model_config = ConfigDict(extra='forbid', strict=True)
    origin: str = Field(max_length=4096, description='Explicit exact HTTP(S) origin, including port; no subdomains.')
    path_prefix: str = Field(max_length=4096, description='Explicit path subtree; / opts into the whole exact origin.')
    max_pages: int = Field(default=5, ge=1, le=20, description='Attempted pages including seed, failures and cache hits.')
    max_depth: int = Field(default=1, ge=0, le=3)
    max_seconds: float = Field(default=15, gt=0, le=30, description='Also bounded by service request deadline.')
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


def origin(url):
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)


def path(url):
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
    def __init__(self, acquisition, request):
        self.acquisition = acquisition
        self.input = request.traversal
        self.request = request
        self.policy = acquisition.acquisition.policy
        self.scope_origin = origin(self.input.origin)
        self.prefix = path(self.input.path_prefix).rstrip('/')
        self.sources = []
        self.seen = set()
        self.attempted = 0
        self.omissions = Counter()
        self.bytes = 0
        self.strategy = None
        self.current_url = None

    def check_document(self, url):
        url = self.policy.url(url)
        p = path(url)
        if (origin(url) != self.scope_origin
            or (self.prefix and p != self.prefix and not p.startswith(self.prefix + '/'))):
            raise FetchError('scope_rejected')
        return url

    def charge(self, size):
        if self.bytes + size > self.input.max_bytes:
            self.omissions['byte_limit'] += 1
            raise FetchError('traversal_byte_limit')
        self.bytes += size

    async def arun_many(self, urls, config):
        results = []
        for url in urls:
            if url in self.seen:
                continue
            if len(self.sources) >= self.input.max_pages:
                self.omissions['page_limit'] += 1
                self.strategy.cancel()
                break
            self.seen.add(url)
            self.attempted += 1
            self.current_url = url
            try:
                self.check_document(url)
                source = (await self.acquisition.fetch_many([url], freshness=self.request.freshness,
                    max_age_seconds=self.request.max_age_seconds))['sources'][0]
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
                self.strategy.cancel()
            links = source.get('link_targets', [])
            if 'error' not in source and source.get('document_type') == 'html' and 'link_targets' not in source:
                self.omissions['link_metadata_unavailable'] += 1
            if source.get('error', {}).get('code') == 'scope_rejected':
                self.omissions['scope_rejected'] += 1
            if source.get('links_truncated'):
                self.omissions['link_limit'] += 1
            results.append(CrawlResult(url=url, html='', success='error' not in source,
                links={'internal': [{'href': link} for link in links]}, metadata={}))
            if source.get('error', {}).get('code') == 'traversal_byte_limit':
                self.strategy.cancel()
                break
        return results

    async def run(self, seed):
        owner = self
        class ScopedBFS(BFSDeepCrawlStrategy):
            async def link_discovery(self, result, source_url, current_depth, visited, next_level, depths):
                # Supported strategy extension hook: upstream 0.9.4's default
                # discovery drops ref/tracking and blank query parameters. Only
                # fragments are disposable here; inherited BFS still owns levels,
                # result depth/parents and scheduling through arun_many.
                visited.update(owner.seen)
                for link in result.links.get('internal', []):
                    try:
                        url = owner.check_document(urljoin(source_url, link['href']))
                    except FetchError:
                        owner.omissions['scope_rejected'] += 1
                        continue
                    if url in visited:
                        continue
                    if current_depth >= self.max_depth:
                        owner.omissions['depth_limit'] += 1
                        continue
                    if len(owner.sources) + len(next_level) >= owner.input.max_pages:
                        owner.omissions['page_limit'] += 1
                        continue
                    if len(next_level) >= owner.input.max_frontier:
                        owner.omissions['frontier_limit'] += 1
                        continue
                    visited.add(url)
                    next_level.append((url, source_url))
                    depths[url] = current_depth + 1

        self.strategy = ScopedBFS(max_depth=self.input.max_depth, max_pages=self.input.max_pages,
                                 include_external=False)
        token = active_traversal.set(self)
        try:
            try:
                self.scope_origin = origin(self.policy.url(self.input.origin))
                seed = self.check_document(seed)
                async with asyncio.timeout(self.input.max_seconds):
                    await self.strategy.arun(seed, self, CrawlerRunConfig(stream=False))
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
        finally:
            active_traversal.reset(token)
