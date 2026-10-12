"""Application-owned, serialized acquisition; lifecycle refinement belongs to .3."""
import asyncio
from datetime import datetime, timezone
import hashlib
import time
from urllib.parse import urlsplit

import aiohttp
import anyio

from .transport import AcquisitionOptions, DestinationPolicy, FetchError, Transport


class Acquisition:
    def __init__(self, options=None):
        self.options = options or AcquisitionOptions()
        self.policy = DestinationPolicy(self.options)
        self.browser = None
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.active = set()
        self.closed = False
        self.backoff = {}

    async def fetch_many(self, urls):
        sources = []
        for url in urls:
            normalized = None
            try:
                normalized = self.policy.url(url)
                if self.closed:
                    raise FetchError('fetch_failed')
                if self.waiting >= self.options.queue_capacity:
                    raise FetchError('queue_full')
                self.waiting += 1
                try:
                    async with asyncio.timeout(self.options.queue_wait):
                        await self.lock.acquire()
                except TimeoutError:
                    raise FetchError('queue_timeout') from None
                finally:
                    self.waiting -= 1
                task = asyncio.current_task()
                self.active.add(task)
                try:
                    if self.closed:
                        raise FetchError('fetch_failed')
                    async with asyncio.timeout(self.options.deadline):
                        source = await self._acquire(url, normalized)
                    sources.append(source)
                finally:
                    self.active.discard(task)
                    self.lock.release()
            except FetchError as error:
                source = {'error': {'code': error.code}}
                if normalized is not None:
                    source['requested_url'] = normalized
                sources.append(source)
            except TimeoutError:
                sources.append({'requested_url': normalized, 'error': {'code': 'fetch_timeout'}})
            except (aiohttp.ClientError, OSError, ValueError):
                sources.append({'requested_url': normalized, 'error': {'code': 'fetch_failed'}})
        return {'sources': sources, 'content_mode': 'full_text'}

    async def _acquire(self, requested, normalized):
        parts = urlsplit(normalized)
        origin = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80))
        if self.backoff.get(origin, 0) > time.monotonic():
            raise FetchError('rate_limited')
        async with Transport(self.options) as transport:
            try:
                initial = await transport.fetch(normalized)
            except FetchError as error:
                if error.code == 'rate_limited':
                    # No immediate retries or unbounded Retry-After sleeps. At
                    # most one attempt; hold no browser while origin backs off.
                    if len(self.backoff) >= 256:
                        self.backoff = {k: v for k, v in self.backoff.items() if v > time.monotonic()}
                    if len(self.backoff) < 256:
                        self.backoff[origin] = time.monotonic() + 5
                raise
            fetched = datetime.now(timezone.utc).isoformat()
            mime = initial.headers.get('content-type', '').split(';')[0].lower()
            if mime not in ('text/html', 'application/xhtml+xml'):
                raise FetchError('unsupported_format')
            # Known challenge signatures are conservative, not universal detection.
            text = initial.body.lower()
            if any(marker in text for marker in (b'cf-chl-', b'just a moment...',
                b'verify you are human', b'captcha', b'checking your browser')):
                raise FetchError('blocked')
            try:
                if self.browser is None:
                    from .browser import Browser
                    self.browser = Browser()
                title, markdown = await self.browser.render(initial, transport)
            except FetchError:
                raise
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await self.browser.close()
                raise
            except Exception:
                if self.browser is not None:
                    await self.browser.close()
                raise FetchError('browser_unavailable') from None
            if any(marker in markdown.lower() for marker in ('verify you are human', 'just a moment...', 'checking your browser')):
                raise FetchError('blocked')
            return {'requested_url': requested, 'resolved_url': initial.url,
                'title': title, 'document_type': 'html', 'fetched_at': fetched,
                'cache_state': 'miss', 'content_version': hashlib.sha256(markdown.encode()).hexdigest(),
                'content_mode': 'full_text', 'content': markdown}

    async def close(self):
        self.closed = True
        for task in list(self.active):
            task.cancel()
        await asyncio.gather(*list(self.active), return_exceptions=True)
        if self.browser is not None:
            with anyio.CancelScope(shield=True):
                await self.browser.close()
