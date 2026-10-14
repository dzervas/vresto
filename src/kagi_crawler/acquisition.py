"""Shared warmup and idle lifetime, independent of MCP sessions and documents."""
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import logging
import time
from urllib.parse import urlsplit

import anyio

from .types import AcquisitionResult, Source, SourceSuccess, SourceFailure, ErrorCode

from .acquisition_policy import AcquisitionOptions, DestinationPolicy, FetchError
from .browser import Browser, BrowserAdapter
from .pdf import parse_pdf


class Acquisition:
    def __init__(self, options: AcquisitionOptions | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 browser_factory: Callable[[], BrowserAdapter] = Browser) -> None:
        self.options = options or AcquisitionOptions()
        self.policy = DestinationPolicy(self.options)
        self.browser: BrowserAdapter | None = None
        self.browser_factory = browser_factory
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.active = set()
        self.closed = False
        self.backoff = {}
        self.clock = clock
        self.sleep = sleep
        self.lifecycle_lock = asyncio.Lock()
        self.startup = None
        self.idle_task = None
        self.last_work: float | None = None
        self.request_deadline: ContextVar[float | None] = ContextVar('acquisition_deadline', default=None)

    @contextmanager
    def request(self) -> Iterator[None]:
        # Cache/composed requests may call fetch_many one URL at a time. Share
        # the acquisition deadline without making cache hits join its queue.
        token = self.request_deadline.set(asyncio.get_running_loop().time() + self.options.deadline)
        try:
            yield
        finally:
            self.request_deadline.reset(token)

    def warmup(self) -> asyncio.Task[bool] | None:
        # ASGI requests never wait for startup. Keep its result for acquisition;
        # discovery does not consume/reset idle time or hide startup failures.
        if self.closed:
            return None
        if self.startup is None or (self.startup.done() and self.browser is None):
            self.startup = asyncio.create_task(self._warm())
        return self.startup

    async def _warm(self):
        async with self.lifecycle_lock:
            if self.closed:
                return False
            if self.browser is not None:
                return True
            browser = self.browser_factory()
            try:
                async with asyncio.timeout(self.options.deadline):
                    await browser.start()
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await browser.close()
                raise
            except Exception as error:
                self._lifecycle_diagnostic(browser, 'startup_failure', error, 'startup')
                with anyio.CancelScope(shield=True):
                    await browser.close()
                logging.getLogger('kagi_crawler').info('browser_start=failed')
                return False
            logging.getLogger('kagi_crawler').info('browser_start=ready')
            self.browser = browser
            self.last_work = self.clock()
            if self.idle_task is None or self.idle_task.done():
                self.idle_task = asyncio.create_task(self._idle())
            return True

    async def _idle(self):
        assert self.last_work is not None  # _warm starts idle tracking only after startup.
        while not self.closed:
            await self.sleep(max(0, 3600 - (self.clock() - self.last_work)))
            # Taking the acquisition permit protects *all* active work, including
            # native navigation and PDF parsing, from teardown. Recheck after queued work.
            async with self.lock:
                if self.clock() - self.last_work >= 3600:
                    await self._retire()
                    return

    @staticmethod
    def _lifecycle_diagnostic(browser: BrowserAdapter, reason, error, stage):
        try:
            logger = logging.getLogger('kagi_crawler')
            log = logger.info if reason == 'controlled' else logger.warning
            log('browser_retire reason=%s type=%s connected=%s stage=%s',
                reason, type(error).__name__ if error is not None else 'none',
                browser.is_connected(), stage)
        except Exception:
            # A failed probe/handler must not strand a detached owned browser.
            pass

    async def _retire(self, reason='controlled', error=None):
        async with self.lifecycle_lock:
            browser, self.browser = self.browser, None
            if browser is not None:
                self._lifecycle_diagnostic(browser, reason, error, 'acquisition')
                with anyio.CancelScope(shield=True):
                    await browser.close()
                logging.getLogger('kagi_crawler').info('browser_stop=completed')

    async def fetch_many(self, urls: list[str], *, scope: Callable[[str], str] | None = None) -> AcquisitionResult:
        task = asyncio.current_task()
        self.active.add(task)
        try:
            return await self._fetch_many(urls, scope=scope)
        finally:
            self.active.discard(task)

    async def _fetch_many(self, urls: list[str], *, scope: Callable[[str], str] | None = None) -> AcquisitionResult:
        sources: list[Source] = []
        deadline = self.request_deadline.get() or asyncio.get_running_loop().time() + self.options.deadline
        for url in urls:
            normalized = None
            try:
                normalized = self.policy.url(url)
                if self.closed:
                    raise FetchError('fetch_failed')
                if self.waiting >= self.options.queue_capacity:
                    logging.getLogger('kagi_crawler').info('acquisition_queue=full')
                    raise FetchError('queue_full')
                self.waiting += 1
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise FetchError('fetch_timeout')
                    async with asyncio.timeout(min(self.options.queue_wait, remaining)):
                        await self.lock.acquire()
                except TimeoutError:
                    code: ErrorCode = 'fetch_timeout' if asyncio.get_running_loop().time() >= deadline else 'queue_timeout'
                    logging.getLogger('kagi_crawler').info('acquisition_queue=%s', code)
                    raise FetchError(code) from None
                finally:
                    self.waiting -= 1
                try:
                    if self.closed:
                        raise FetchError('fetch_failed')
                    async with asyncio.timeout_at(deadline):
                        source = await self._acquire(url, normalized, scope=scope)
                    sources.append(source)
                except asyncio.CancelledError:
                    # This permit owns cancellation cleanup, even when abandoned
                    # during native navigation or while joining shared startup.
                    with anyio.CancelScope(shield=True):
                        if self.startup is not None and not self.startup.done():
                            self.startup.cancel()
                            await asyncio.gather(self.startup, return_exceptions=True)
                        await self._retire()
                    raise
                finally:
                    # Completion includes PDF parsing, not just native download.
                    # Start a full idle interval before releasing ownership.
                    if self.browser is not None:
                        self.last_work = self.clock()
                    self.lock.release()
            except FetchError as error:
                failure: SourceFailure = {'error': {'code': error.code, **error.details}}
                if normalized is not None:
                    failure['requested_url'] = normalized
                sources.append(failure)
            except TimeoutError:
                sources.append({'requested_url': normalized, 'error': {'code': 'fetch_timeout'}})
            except (OSError, ValueError):
                sources.append({'requested_url': normalized, 'error': {'code': 'fetch_failed'}})
        return {'sources': sources, 'content_mode': 'full_text'}

    async def _acquire(self, requested: str, normalized: str, *, scope: Callable[[str], str] | None = None) -> SourceSuccess:
        parts = urlsplit(normalized)
        origin = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80))
        if self.backoff.get(origin, 0) > time.monotonic():
            raise FetchError('rate_limited')
        try:
            startup = self.startup
            if startup is None or (startup.done() and not startup.cancelled() and startup.result() and self.browser is None):
                startup = self.warmup()
            if startup is None or not await asyncio.shield(startup):
                raise FetchError('browser_unavailable')
            self.last_work = self.clock()
            assert self.browser is not None  # Successful startup owns a live browser.
            result = await self.browser.acquire(normalized, self.options)
        except FetchError as error:
            if error.code == 'rate_limited':
                if len(self.backoff) >= 256:
                    self.backoff = {k: v for k, v in self.backoff.items() if v > time.monotonic()}
                if len(self.backoff) < 256:
                    self.backoff[origin] = time.monotonic() + 5
            if error.code == 'browser_unavailable':
                await self._retire('browser_unavailable', error)
            raise
        except Exception as error:
            await self._retire('unexpected_exception', error)
            raise FetchError('browser_unavailable') from None
        # Final URL screening is post-acquisition. Native redirects have already
        # connected: this is evidence/traversal scope, never a socket firewall.
        if scope is not None:
            scope(result['url'])
        fetched = datetime.now(timezone.utc).isoformat()
        if 'pdf' in result:
            title, content, pdf = await parse_pdf(result['pdf'], self.options)
            extras = {'pdf': pdf}
            document_type = 'pdf'
        else:
            title, content = result['title'], result['content']
            extras = {'link_targets': result['links'], 'links_truncated': result['links_truncated'],
                      'coverage': result['coverage']}
            document_type = 'html'
        return cast(SourceSuccess, {'requested_url': requested, 'resolved_url': result['url'],
            'title': title, 'document_type': document_type, 'fetched_at': fetched,
            'cache_state': 'miss', 'content_version': hashlib.sha256(content.encode()).hexdigest(),
            'content_mode': 'full_text', 'content': content, **extras})

    async def close(self) -> None:
        self.closed = True
        for task in list(self.active):
            task.cancel()
        await asyncio.gather(*list(self.active), return_exceptions=True)
        tasks: list[asyncio.Task[Any]] = [t for t in (self.startup, self.idle_task) if t is not None]
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._retire()
