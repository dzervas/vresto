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
        # Admit origin and tab capacity atomically: origin waiters consume no tab.
        self.admission = asyncio.Condition()
        self.origins: set[tuple[str, str | None, int]] = set()
        self.active = 0
        self.retiring = False
        self.tasks: set[asyncio.Task[Any]] = set()
        self.pdf_lock = asyncio.Lock()
        self.browser_failed = asyncio.Event()
        self.waiting = 0
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
        token = self.request_deadline.set(self.request_deadline.get() or
                                          asyncio.get_running_loop().time() + self.options.deadline)
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
            self.browser_failed = asyncio.Event()
            self.browser = browser
            self.last_work = self.clock()
            if self.idle_task is None or self.idle_task.done():
                self.idle_task = asyncio.create_task(self._idle())
            return True

    async def _idle(self):
        assert self.last_work is not None  # _warm starts idle tracking only after startup.
        while not self.closed:
            await self.sleep(max(0, 3600 - (self.clock() - self.last_work)))
            async with self.admission:
                await self.admission.wait_for(lambda: self.active == 0)
                if self.clock() - self.last_work < 3600:
                    continue
                # Pause admission, but keep waiters counted/timed during teardown.
                # Warmup also uses the lifecycle lock; a replacement gets its
                # own idle watcher, never inherits this expired interval.
                self.retiring = True
                self.idle_task = None
                browser = self.browser
            try:
                if browser is not None:
                    await self._retire(expected=browser)
            finally:
                with anyio.CancelScope(shield=True):
                    async with self.admission:
                        self.retiring = False
                        self.admission.notify_all()
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

    async def _retire(self, reason='controlled', error=None, *, expected: BrowserAdapter | None = None):
        async with self.lifecycle_lock:
            if expected is not None and self.browser is not expected:
                return  # A peer already retired this generation, never its replacement.
            browser, self.browser = self.browser, None
            if browser is not None:
                self.browser_failed.set()
                self._lifecycle_diagnostic(browser, reason, error, 'acquisition')
                with anyio.CancelScope(shield=True):
                    await browser.close()
                logging.getLogger('kagi_crawler').info('browser_stop=completed')

    async def fetch_many(self, urls: list[str], *, scope: Callable[[str], str] | None = None) -> AcquisitionResult:
        # TaskGroup drains every owned page on cancellation, including callers
        # using AnyIO cancellation scopes. Results remain in input order.
        deadline = self.request_deadline.get() or asyncio.get_running_loop().time() + self.options.deadline
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._fetch(url, deadline, scope=scope)) for url in urls]
        return {'sources': [task.result() for task in tasks], 'content_mode': 'full_text'}

    @staticmethod
    def _origin(url: str) -> tuple[str, str | None, int]:
        parts = urlsplit(url)
        return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)

    async def _admit(self, origin: tuple[str, str | None, int], deadline: float) -> None:
        async with self.admission:
            if self.closed:
                raise FetchError('fetch_failed')
            if asyncio.get_running_loop().time() >= deadline:
                raise FetchError('fetch_timeout')
            def available():
                return (not self.retiring and self.active < self.options.browser_concurrency
                        and origin not in self.origins)
            if not available():
                if self.waiting >= self.options.queue_capacity:
                    logging.getLogger('kagi_crawler').info('acquisition_queue=full')
                    raise FetchError('queue_full')
                self.waiting += 1
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    async with asyncio.timeout(min(self.options.queue_wait, remaining)):
                        await self.admission.wait_for(lambda: self.closed or available())
                except TimeoutError:
                    code: ErrorCode = 'fetch_timeout' if asyncio.get_running_loop().time() >= deadline else 'queue_timeout'
                    logging.getLogger('kagi_crawler').info('acquisition_queue=%s', code)
                    raise FetchError(code) from None
                finally:
                    self.waiting -= 1
            if self.closed:
                raise FetchError('fetch_failed')
            self.active += 1
            self.origins.add(origin)

    async def _fetch(self, url: str, deadline: float, *, scope: Callable[[str], str] | None) -> Source:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        normalized = None
        try:
            normalized = self.policy.url(url)
            origin = self._origin(normalized)
            await self._admit(origin, deadline)
            try:
                async with asyncio.timeout_at(deadline):
                    return await self._acquire(url, normalized, scope=scope)
            finally:
                with anyio.CancelScope(shield=True):
                    async with self.admission:
                        # Completion includes PDF parsing, not just the download.
                        if self.browser is not None:
                            self.last_work = self.clock()
                        self.active -= 1
                        self.origins.remove(origin)
                        self.admission.notify_all()
        except FetchError as error:
            failure: SourceFailure = {'error': {'code': error.code, **error.details}}
            if normalized is not None:
                failure['requested_url'] = normalized
            return failure
        except TimeoutError:
            return {'requested_url': normalized, 'error': {'code': 'fetch_timeout'}}
        except (OSError, ValueError):
            return {'requested_url': normalized, 'error': {'code': 'fetch_failed'}}
        finally:
            self.tasks.discard(task)

    async def _connected_acquire(self, browser: BrowserAdapter, failed: asyncio.Event,
                                 requested: str, normalized: str, scope: Callable[[str], str] | None):
        # The generation owns all in-flight work, including queued/active PDF
        # parsing. A crash drains these too, not only native navigation.
        work = asyncio.create_task(self._document(browser, requested, normalized, scope))
        failure = asyncio.create_task(failed.wait())
        try:
            await asyncio.wait((work, failure), return_when=asyncio.FIRST_COMPLETED)
            if failed.is_set():
                raise FetchError('browser_unavailable')
            return work.result()
        finally:
            work.cancel()
            failure.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(work, failure, return_exceptions=True)

    async def _acquire(self, requested: str, normalized: str, *, scope: Callable[[str], str] | None = None) -> SourceSuccess:
        origin = self._origin(normalized)
        if self.backoff.get(origin, 0) > time.monotonic():
            raise FetchError('rate_limited')
        startup = self.startup
        if startup is None or (startup.done() and not startup.cancelled() and startup.result() and self.browser is None):
            startup = self.warmup()
        if startup is None or not await asyncio.shield(startup):
            raise FetchError('browser_unavailable')
        browser = self.browser
        if browser is None:
            raise FetchError('browser_unavailable')
        failed = self.browser_failed
        try:
            return await self._connected_acquire(browser, failed, requested, normalized, scope)
        except FetchError as error:
            if error.code == 'rate_limited':
                if len(self.backoff) >= 256:
                    self.backoff = {k: v for k, v in self.backoff.items() if v > time.monotonic()}
                if len(self.backoff) < 256:
                    self.backoff[origin] = time.monotonic() + 5
            if error.code == 'browser_unavailable':
                failed.set()
                await self._retire('browser_unavailable', error, expected=browser)
            raise
        except Exception as error:
            # Conversion/tab failures must not disrupt connected peer tabs.
            try:
                connected = browser.is_connected()
            except Exception:
                connected = False
            if connected:
                raise FetchError('fetch_failed') from None
            failed.set()
            await self._retire('unexpected_exception', error, expected=browser)
            raise FetchError('browser_unavailable') from None

    async def _document(self, browser: BrowserAdapter, requested: str, normalized: str,
                        scope: Callable[[str], str] | None) -> SourceSuccess:
        result = await browser.acquire(normalized, self.options)
        # Final URL screening is post-acquisition. Native redirects have already
        # connected: this is evidence/traversal scope, never a socket firewall.
        if scope is not None:
            scope(result['url'])
        fetched = datetime.now(timezone.utc).isoformat()
        if 'pdf' in result:
            async with self.pdf_lock:
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
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        tasks: list[asyncio.Task[Any]] = [t for t in (self.startup, self.idle_task) if t is not None]
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._retire()
