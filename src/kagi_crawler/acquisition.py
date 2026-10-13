"""Shared warmup and idle lifetime, independent of MCP sessions and documents."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import logging
import time
from urllib.parse import urlsplit

import anyio

from .transport import AcquisitionOptions, DestinationPolicy, FetchError
from .browser import Browser
from .pdf import parse_pdf


class Acquisition:
    def __init__(self, options=None, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.options = options or AcquisitionOptions()
        self.policy = DestinationPolicy(self.options)
        self.browser = None
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
        self.last_work = None
        self.request_deadline = ContextVar('acquisition_deadline', default=None)

    @contextmanager
    def request(self):
        # Cache/composed requests may call fetch_many one URL at a time. Share
        # the acquisition deadline without making cache hits join its queue.
        token = self.request_deadline.set(asyncio.get_running_loop().time() + self.options.deadline)
        try:
            yield
        finally:
            self.request_deadline.reset(token)

    def warmup(self):
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
            browser = Browser()
            try:
                async with asyncio.timeout(self.options.deadline):
                    await browser.start()
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await browser.close()
                raise
            except Exception:
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
        while not self.closed:
            await self.sleep(max(0, 3600 - (self.clock() - self.last_work)))
            # Taking the acquisition permit protects *all* active work, including
            # native navigation and PDF parsing, from teardown. Recheck after queued work.
            async with self.lock:
                if self.clock() - self.last_work >= 3600:
                    await self._retire()
                    return

    async def _retire(self):
        async with self.lifecycle_lock:
            browser, self.browser = self.browser, None
            if browser is not None:
                with anyio.CancelScope(shield=True):
                    await browser.close()
                logging.getLogger('kagi_crawler').info('browser_stop=completed')

    async def fetch_many(self, urls):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            return await self._fetch_many(urls)
        finally:
            self.active.discard(task)

    async def _fetch_many(self, urls):
        sources = []
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
                    code = 'fetch_timeout' if asyncio.get_running_loop().time() >= deadline else 'queue_timeout'
                    logging.getLogger('kagi_crawler').info('acquisition_queue=%s', code)
                    raise FetchError(code) from None
                finally:
                    self.waiting -= 1
                try:
                    if self.closed:
                        raise FetchError('fetch_failed')
                    async with asyncio.timeout_at(deadline):
                        source = await self._acquire(url, normalized)
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
                source = {'error': {'code': error.code, **error.details}}
                if normalized is not None:
                    source['requested_url'] = normalized
                sources.append(source)
            except TimeoutError:
                sources.append({'requested_url': normalized, 'error': {'code': 'fetch_timeout'}})
            except (OSError, ValueError):
                sources.append({'requested_url': normalized, 'error': {'code': 'fetch_failed'}})
        return {'sources': sources, 'content_mode': 'full_text'}

    async def _acquire(self, requested, normalized):
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
            result = await self.browser.acquire(normalized, self.options)
        except FetchError as error:
            if error.code == 'rate_limited':
                if len(self.backoff) >= 256:
                    self.backoff = {k: v for k, v in self.backoff.items() if v > time.monotonic()}
                if len(self.backoff) < 256:
                    self.backoff[origin] = time.monotonic() + 5
            if error.code == 'browser_unavailable':
                await self._retire()
            raise
        except Exception:
            await self._retire()
            raise FetchError('browser_unavailable') from None
        # Final URL screening is post-acquisition. Native redirects have already
        # connected: this is evidence/traversal scope, never a socket firewall.
        from .traversal import active_traversal
        traversal = active_traversal.get()
        if traversal is not None:
            traversal.check_document(result['url'])
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
        return {'requested_url': requested, 'resolved_url': result['url'],
            'title': title, 'document_type': document_type, 'fetched_at': fetched,
            'cache_state': 'miss', 'content_version': hashlib.sha256(content.encode()).hexdigest(),
            'content_mode': 'full_text', 'content': content, **extras}

    async def close(self):
        self.closed = True
        for task in list(self.active):
            task.cancel()
        await asyncio.gather(*list(self.active), return_exceptions=True)
        tasks = [t for t in (self.startup, self.idle_task) if t is not None]
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._retire()
