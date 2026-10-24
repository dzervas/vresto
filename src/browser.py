"""Native Chromium networking; no proxy, routing or separate HTTP client.

Chromium owns all networking. DoH encrypts DNS,
not an SSRF boundary; deployment egress, memory and disk limits are required.
"""
from typing import Protocol, cast
import asyncio
import logging
from pathlib import Path

import anyio
from patchright.async_api import Error, TimeoutError as BrowserTimeout

from patchright.async_api import Browser as NativeBrowser
from .chromium import Chromium
from .types import BrowserResult, SourceDiagnostic
from .acquisition_policy import AcquisitionOptions

from .acquisition_policy import FetchError
from .content_expansion import DOM_WITHIN_LIMIT
from .sites import expand_content, extract_document
from .native_observation import NativeLoadObservation
from .deadline import remaining

# Inspect the live page, not occurrences in documentation, scripts or hidden
# CAPTCHA widgets. The cf-mitigated response header is checked separately.
_CHALLENGE_PAGE = r'''() => {
    const prompt = /^(just a moment[.!…]*|checking your browser[.!…]*|verify you are human[.!…]*|security verification[.!…]*|attention required!\s*\|\s*cloudflare)$/i;
    const title = document.title.trim();
    const form = document.querySelector('#challenge-form, #cf-challenge-running');
    const visibleForm = form && form.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
    const heading = document.querySelector('h1');
    return Boolean((prompt.test(title) &&
        (visibleForm || (document.body?.innerText.length || 0) < 2000)) ||
        (visibleForm && heading && prompt.test(heading.textContent.trim())));
}'''

class BrowserAdapter(Protocol):
    """Acquisition's entire browser seam; native objects stay inside the adapter."""

    async def start(self) -> None: ...
    async def acquire(self, url: str, options: AcquisitionOptions) -> BrowserResult: ...
    async def close(self) -> None: ...
    def is_connected(self) -> bool: ...


class Browser:
    def __init__(self) -> None:
        self.runtime = Chromium()
        self.start_lock = asyncio.Lock()

    def _diagnostic(self, error, stage, reason):
        # Only fixed phases/reasons, exception classes and connection state.
        try:
            logging.getLogger('vresto').warning(
                'acquisition_failure stage=%s type=%s connected=%s reason=%s',
                stage, type(error).__name__,
                self.is_connected(), reason)
        except Exception:
            # Diagnostics must not replace failures or prevent owned cleanup.
            pass

    def is_connected(self) -> bool:
        return self.runtime.browser is not None and self.runtime.browser.is_connected()

    async def start(self) -> None:
        async with self.start_lock:
            if self.runtime.browser is not None:
                if not self.is_connected():
                    raise FetchError('browser_unavailable')
                return
            try:
                await self.runtime.start()
            except BaseException as error:
                if isinstance(error, Exception):
                    self._diagnostic(error, 'startup', 'startup_failure')
                with anyio.CancelScope(shield=True):
                    await self.close()
                raise

    async def acquire(self, url: str, options: AcquisitionOptions) -> BrowserResult:
        await self.start()
        state = {'challenge': False, 'http_status': None,
                 'deadline': asyncio.get_running_loop().time() + remaining(options.deadline),
                 'stage': 'context', 'diagnosed': False}
        try:
            # One duration across navigation, native challenge continuation,
            # JS settling, downloads and conversion. Never extend the deadline.
            async with asyncio.timeout_at(state['deadline']):
                return await self._acquire(url, options, state)
        except TimeoutError as error:
            if not state['diagnosed']:
                self._diagnostic(error, state['stage'], 'deadline')
            if state['challenge']:
                raise FetchError('blocked', reason='challenge_timeout',
                                 http_status=state['http_status'],
                                 diagnostics={'stage': state['stage'], 'reason': 'deadline'}) from None
            raise FetchError('fetch_timeout', diagnostics={'stage': state['stage'], 'reason': 'deadline'}) from None
        except FetchError as error:
            error.diagnostics.setdefault('stage', state['stage'])
            if state['http_status'] is not None:
                error.diagnostics.setdefault('http_status', state['http_status'])
            raise
        except Error as error:
            # Tab creation is outside _acquire's conversion handler; preserve
            # its native propagation and identify the type here too.
            if not state['diagnosed']:
                reason = 'native_timeout' if isinstance(error, BrowserTimeout) else 'native_error'
                self._diagnostic(error, state['stage'], reason)
            raise
        except Exception as error:
            if not state['diagnosed']:
                self._diagnostic(error, state['stage'], 'unexpected_exception')
            raise

    @staticmethod
    def _check_status(response):
        if response.status < 400:
            return
        diagnostic: SourceDiagnostic = {'reason': 'http_status', 'http_status': response.status, 'stage': 'http_response'}
        if response.status == 429:
            raise FetchError('rate_limited', diagnostics=diagnostic)
        if response.status in (401, 403):
            raise FetchError('blocked', reason='http_status', http_status=response.status, diagnostics=diagnostic)
        raise FetchError('fetch_failed', diagnostics=diagnostic)

    @staticmethod
    def _replaced(error: Exception) -> bool:
        # Probes run right after DOMContentLoaded, so a native replacement (e.g.
        # challenge continuation) can destroy the probed document. Callers loop
        # and revalidate the new response under the same deadline.
        return isinstance(error, Error) and 'Execution context was destroyed' in str(error)

    async def _settled_response(self, page, latest, state):
        while True:
            response = latest()
            if response is None:
                raise FetchError('fetch_failed')
            # Redirect responses are not committed documents. In particular,
            # Cloudflare can redirect between consecutive challenge pages.
            if response.status in (301, 302, 303, 307, 308):
                await asyncio.sleep(0.1)
                continue
            state['http_status'] = response.status
            if response.status == 429:
                self._check_status(response)
            mime = response.headers.get('content-type', '').split(';')[0].lower()
            challenged = response.headers.get('cf-mitigated') == 'challenge'
            if not challenged and mime in ('text/html', 'application/xhtml+xml'):
                await page.wait_for_load_state('domcontentloaded', timeout=0)
                try:
                    challenged = await page.evaluate(_CHALLENGE_PAGE)
                except Error as error:
                    if not self._replaced(error):
                        raise
                    await asyncio.sleep(0.1)
                    continue
                # A native navigation may have replaced this response during
                # the DOM probe. Inspect the new one before accepting evidence.
                if latest() is not response:
                    continue
            state['challenge'] = challenged
            if not challenged:
                self._check_status(response)
                return response
            # Let native JavaScript/cookies redirect automatically. No CAPTCHA
            # clicking, request interception, fingerprint injection or retries.
            await asyncio.sleep(0.1)

    async def _settle_rendered(self, page, latest, state, options, response, pending):
        # Observe network idle concurrently: it is informational, not a prerequisite
        # for a quiet DOM with no tracked fetch/XHR. Only not-quiet/pending loads
        # at the 3 s cap imply partial coverage; the acquisition deadline still wins.
        idle = False
        async def observe_idle():
            nonlocal idle
            try:
                await page.wait_for_load_state('networkidle', timeout=1000)
                idle = True
            except Error:
                # Informational only: a timeout or a replaced document must not
                # replace the settling outcome.
                pass
        idle_task = asyncio.create_task(observe_idle())
        started = asyncio.get_running_loop().time()
        changed = started
        previous = None
        try:
            # Performance follow-up: an in-page MutationObserver could avoid polling
            # innerText (layout on large pages) every 100 ms and react sooner. Measure
            # whether a 200–250 ms quiet window preserves identical content.
            while True:
                if await self._settled_response(page, latest, state) is not response:
                    return False, idle, False, bool(pending)
                try:
                    within = await page.evaluate(DOM_WITHIN_LIMIT, options.dom_bytes)
                    text = await page.evaluate('() => document.body?.innerText || ""')
                except Error as error:
                    if not self._replaced(error):
                        raise
                    await asyncio.sleep(0.1)
                    continue
                if not within:
                    raise FetchError('resource_limit')
                now = asyncio.get_running_loop().time()
                if text != previous:
                    previous, changed = text, now
                if not pending and now - changed >= 0.4:
                    return True, idle, True, False
                if now - started >= 3:
                    return True, idle, now - changed >= 0.4, bool(pending)
                await asyncio.sleep(0.1)
        finally:
            idle_task.cancel()
            # Drain on success, revalidation, errors and caller cancellation.
            with anyio.CancelScope(shield=True):
                await asyncio.gather(idle_task, return_exceptions=True)
            if not idle_task.cancelled():
                idle_task.result()

    @staticmethod
    async def _download(download, options) -> BrowserResult:
        filename = await download.path()
        if filename is None or await download.failure():
            raise FetchError('fetch_failed')
        path = Path(filename)
        if path.stat().st_size > options.response_bytes:
            raise FetchError('resource_limit')
        data = path.read_bytes()
        if not data.startswith(b'%PDF-'):
            raise FetchError('unsupported_format')
        return {'url': download.url, 'pdf': data}

    async def _navigate(self, page, url, options, state):
        download_ready = asyncio.get_running_loop().create_future()
        def downloaded(download):
            if not download_ready.done():
                download_ready.set_result(download)
        page.on('download', downloaded)
        navigation_response = None
        def responded(response):
            nonlocal navigation_response
            if (response.request.is_navigation_request() and response.request.frame == page.main_frame
                    and response.status not in (301, 302, 303, 307, 308)):
                navigation_response = response
                state['http_status'] = response.status
                state['challenge'] = response.headers.get('cf-mitigated') == 'challenge'
        page.on('response', responded)
        state['stage'] = 'navigation'
        try:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=0)
        except Error as error:
            if navigation_response is not None and not state['challenge']:
                self._check_status(navigation_response)
            if 'Download is starting' in str(error):
                if navigation_response is not None:
                    self._check_status(navigation_response)
                    if state['challenge']:
                        # A downloaded challenge cannot execute JavaScript;
                        # never turn its bytes into canonical PDF evidence.
                        raise FetchError('blocked', reason='challenge_download',
                                         http_status=navigation_response.status)
                return None, download_ready
            if not state['challenge'] or not self.is_connected():
                raise
            response = navigation_response
        def latest():
            return navigation_response or response
        return latest, download_ready

    async def _render_document(self, page, latest, download_ready, native_loads, options, state) -> BrowserResult:
        coverage = None
        while True:
            state['stage'] = 'response_validation'
            response = await self._settled_response(page, latest, state)
            mime = response.headers.get('content-type', '').split(';')[0].lower()
            if mime not in ('text/html', 'application/xhtml+xml'):
                if mime in ('application/pdf', 'application/octet-stream') or download_ready.done():
                    return await self._download(await download_ready, options)
                data = await response.body()
                if len(data) > options.response_bytes:
                    raise FetchError('resource_limit')
                if data.startswith(b'%PDF-'):
                    return {'url': response.url, 'pdf': data}
                raise FetchError('unsupported_format')
            state['stage'] = 'render'
            settled, idle, quiet, pending_load = await self._settle_rendered(page, latest, state, options, response, native_loads.pending)
            if not settled:
                continue
            if coverage is None:
                coverage = await self._expand(page, latest, state, options, response, idle, quiet, pending_load)
            if not quiet or pending_load:
                coverage['partial'] = True
                if coverage['stop_reason'] in ('settled', 'no_progress'):
                    coverage['stop_reason'] = 'render_settling_limit'
            state['stage'] = 'response_validation'
            if await self._settled_response(page, latest, state) is response:
                break
        return await self._serialize_document(page, options, state, coverage)

    async def _expand(self, page, latest, state, options, response, idle, quiet, pending_load):
        # Reserve serialization/cleanup time. Page-controlled native
        # expansion can replace the document: validate it again,
        # including challenge continuation, HTTP errors and downloads,
        # under the original deadline without expanding it again.
        async def revalidate():
            return await self._settled_response(page, latest, state) is response
        state['stage'] = 'expansion'
        coverage = await expand_content(page, options, deadline=state['deadline'] - 0.15,
                                        revalidate=revalidate)
        coverage['initial_render'] = {'network_idle': idle, 'dom_quiet': quiet,
                                      'pending_native_load': pending_load,
                                      'partial': not quiet or pending_load}
        return coverage

    async def _serialize_document(self, page, options, state, coverage) -> BrowserResult:
        # Bound DOM serialization, not browser allocations/resource traffic.
        state['stage'] = 'dom_serialization'
        within = await page.evaluate(DOM_WITHIN_LIMIT, options.dom_bytes)
        if not within:
            raise FetchError('resource_limit')
        html = await page.content()
        final_url = page.url
        if len(html.encode()) > options.dom_bytes:
            raise FetchError('resource_limit')
        state['stage'] = 'extraction'
        extracted = extract_document(html, final_url, coverage=coverage)
        markdown = extracted.markdown
        if len(markdown.encode()) > options.response_bytes:
            raise FetchError('resource_limit')
        if not markdown.strip():
            raise FetchError('unsupported_format')
        state['stage'] = 'metadata'
        return {'url': final_url, 'title': await page.title(), 'content': markdown,
                'links': extracted.links,
                'links_truncated': extracted.links_truncated, 'coverage': coverage}

    async def _acquire(self, url, options, state) -> BrowserResult:
        browser = cast(NativeBrowser, self.runtime.browser)  # acquire() awaits successful start().
        # Each acquisition owns only its page/window; shared state survives until retirement.
        page = await self.runtime.new_page()
        native_loads = None
        try:
            native_loads = NativeLoadObservation(page)
            latest, download_ready = await self._navigate(page, url, options, state)
            if latest is None:
                return await self._download(await download_ready, options)
            return await self._render_document(page, latest, download_ready, native_loads, options, state)
        except BrowserTimeout as error:
            self._diagnostic(error, state['stage'], 'native_timeout')
            state['diagnosed'] = True
            if state['challenge']:
                raise FetchError('blocked', reason='challenge_timeout',
                                 http_status=state['http_status'], diagnostics={'reason': 'native_timeout'}) from None
            raise FetchError('fetch_timeout', diagnostics={'reason': 'native_timeout'}) from None
        except Error as error:
            self._diagnostic(error, state['stage'], 'native_error')
            state['diagnosed'] = True
            code = 'fetch_failed' if browser.is_connected() else 'browser_unavailable'
            raise FetchError(code, diagnostics={'reason': 'native_error'}) from None
        finally:
            previous_stage = state['stage']
            state['stage'] = 'cleanup'
            try:
                if native_loads is not None:
                    native_loads.close()
                with anyio.CancelScope(shield=True):
                    await page.close()
            except Exception as error:
                self._diagnostic(error, 'cleanup', 'cleanup_failure')
                state['diagnosed'] = True
                raise
            else:
                # Successful cleanup must not relabel the primary failure.
                state['stage'] = previous_stage

    async def close(self) -> None:
        await self.runtime.close()
