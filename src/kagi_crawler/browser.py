"""Native Chromium networking; no proxy, routing or separate HTTP client.

User-approved departure from the historical interception spec. DoH encrypts DNS,
not an SSRF boundary; deployment egress, memory and disk limits are required.
"""
import asyncio
import os
import signal
from pathlib import Path
import sys
import tempfile
import json
from html.parser import HTMLParser
from urllib.parse import urljoin

import anyio
import patchright
from patchright.async_api import Error, TimeoutError as BrowserTimeout
from crawl4ai import CrawlerRunConfig
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from .transport import FetchError
from .content_expansion import DOM_WITHIN_LIMIT, expand_content
from .normalization import main_content_html

_DRIVER_START_LOCK = asyncio.Lock()
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

_RUNTIME_ENV = ('PATH', 'HOME', 'TMPDIR', 'LD_LIBRARY_PATH', 'DISPLAY', 'LANG',
                'PLAYWRIGHT_BROWSERS_PATH', 'FONTCONFIG_FILE', 'FONTCONFIG_PATH')


class LinkTargets(HTMLParser):
    """Keep original anchor queries: Crawl4AI link metadata drops ref/utm keys."""
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.base = url
        self.has_base = False
        self.targets = []
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get('href')
        if not href or len(href) > 4096:
            return
        if tag == 'base' and not self.has_base:
            self.base = urljoin(self.base, href)
            self.has_base = True
        if tag == 'a':
            if len(self.targets) < 256:
                self.targets.append(href)
            else:
                self.truncated = True


class Browser:
    def __init__(self):
        self.headful = os.environ.get('KAGI_CRAWLER_HEADFUL') == '1'
        self.channel = os.environ.get('KAGI_CRAWLER_BROWSER_CHANNEL', 'chromium')
        if self.channel not in ('chromium', 'chrome'):
            raise ValueError('browser channel must be chromium or chrome')
        self.browser = None
        self.profile = None
        self.start_lock = asyncio.Lock()
        self.driver_pid = None
        self.driver_context = None

    async def start(self):
        async with self.start_lock:
            if self.browser is not None:
                if not self.browser.is_connected():
                    raise FetchError('browser_unavailable')
                return
            # Pinned Chrome 153 default_dns_over_https_config_source.cc reads
            # browser-wide Local State, not profile prefs or invented DoH flags.
            # Full Chrome (not headless shell) provides this configuration seam.
            # Acquisitions use separate incognito contexts, not this fresh,
            # owned bootstrap profile, deleted when the browser retires.
            self.profile = tempfile.TemporaryDirectory(prefix='crawler-chrome-')
            Path(self.profile.name, 'Local State').write_text(json.dumps({
                'dns_over_https': {'mode': 'secure', 'templates': 'https://1.1.1.1/dns-query'}}))
            Path(self.profile.name, 'Default').mkdir()
            Path(self.profile.name, 'Default', 'Preferences').write_text(json.dumps({
                'plugins': {'always_open_pdf_externally': True}}))
            try:
                async with _DRIVER_START_LOCK:
                    node = os.environ.get('PLAYWRIGHT_NODEJS_PATH') or str(Path(patchright.__file__).parent / 'driver' / 'node')
                    safe_env = {k: os.environ[k] for k in _RUNTIME_ENV if k in os.environ}
                    with tempfile.TemporaryDirectory(prefix='crawler-driver-') as directory:
                        wrapper = Path(directory) / 'node'
                        pidfile = Path(directory) / 'pid'
                        # Own startup's process group before the driver handshake.
                        wrapper.write_text(f'#!{sys.executable}\nimport os, sys\nos.setsid()\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\nos.execve({node!r}, [{node!r}, *sys.argv[1:]], {safe_env!r})\n')
                        wrapper.chmod(0o700)
                        previous = os.environ.get('PLAYWRIGHT_NODEJS_PATH')
                        os.environ['PLAYWRIGHT_NODEJS_PATH'] = str(wrapper)
                        from patchright.async_api import async_playwright
                        self.driver_context = async_playwright()
                        try:
                            playwright = await self.driver_context.start()
                            bootstrap = await playwright.chromium.launch_persistent_context(
                                self.profile.name, channel=self.channel, headless=not self.headful,
                                no_viewport=self.headful, env=safe_env, ignore_https_errors=False)
                            # Keep Patchright's own defaults: adding back
                            # --disable-component-update contradicts its patches.
                            self.browser = bootstrap.browser
                            control = await bootstrap.new_page()
                            await control.goto('chrome://settings/security')
                            # Chrome 153 settings_secure_dns_handler.cc computes
                            # the effective resolver setting, including policy.
                            # WebUI chrome.send exists only in the main world;
                            # Patchright evaluate deliberately uses an isolated one.
                            cdp = await bootstrap.new_cdp_session(control)
                            probe = await cdp.send('Runtime.evaluate', {
                                'expression': '''(async () => {
                                    const {sendWithPromise} = await import('chrome://resources/js/cr.js');
                                    return await sendWithPromise('getSecureDnsSetting');
                                })()''', 'awaitPromise': True, 'returnByValue': True})
                            await cdp.detach()
                            effective = probe.get('result', {}).get('value', {})
                            if effective.get('mode') != 'secure' or effective.get('config') != 'https://1.1.1.1/dns-query':
                                raise FetchError('browser_unavailable')
                            await control.close()
                        finally:
                            if self.driver_context is not None and not pidfile.exists():
                                with anyio.CancelScope(shield=True):
                                    for _ in range(20):
                                        if pidfile.exists():
                                            break
                                        await asyncio.sleep(0.01)
                            if pidfile.exists():
                                self.driver_pid = int(pidfile.read_text())
                            if previous is None:
                                os.environ.pop('PLAYWRIGHT_NODEJS_PATH', None)
                            else:
                                os.environ['PLAYWRIGHT_NODEJS_PATH'] = previous
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self.close()
                raise

    async def acquire(self, url, options):
        await self.start()
        state = {'challenge': False, 'http_status': None,
                 'deadline': asyncio.get_running_loop().time() + options.request_timeout}
        try:
            # One duration across navigation, native challenge continuation,
            # JS settling, downloads and conversion. Never extend the deadline.
            async with asyncio.timeout(options.request_timeout):
                return await self._acquire(url, options, state)
        except TimeoutError:
            if state['challenge']:
                raise FetchError('blocked', reason='challenge_timeout',
                                 http_status=state['http_status']) from None
            raise FetchError('fetch_timeout') from None

    @staticmethod
    def _check_status(response):
        if response.status == 429:
            raise FetchError('rate_limited')
        if response.status in (401, 403):
            raise FetchError('blocked', reason='http_status', http_status=response.status)
        if response.status >= 400:
            raise FetchError('fetch_failed')

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
                raise FetchError('rate_limited')
            mime = response.headers.get('content-type', '').split(';')[0].lower()
            challenged = response.headers.get('cf-mitigated') == 'challenge'
            if not challenged and mime in ('text/html', 'application/xhtml+xml'):
                await page.wait_for_load_state('domcontentloaded')
                challenged = await page.evaluate(_CHALLENGE_PAGE)
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
        # Long-lived native resources need not prevent reading public text. Give
        # normal rendering its network-idle window, then require a quiet DOM
        # window instead; neither wait extends the original acquisition deadline.
        idle = True
        try:
            await page.wait_for_load_state('networkidle', timeout=min(1000, options.request_timeout * 1000))
        except BrowserTimeout:
            idle = False
        if idle and not pending:
            return await self._settled_response(page, latest, state) is response, True, True, False
        started = asyncio.get_running_loop().time()
        changed = started
        previous = None
        while True:
            if await self._settled_response(page, latest, state) is not response:
                return False, idle, False, bool(pending)
            if not await page.evaluate(DOM_WITHIN_LIMIT, options.dom_bytes):
                raise FetchError('resource_limit')
            text = await page.evaluate('() => document.body?.innerText || ""')
            now = asyncio.get_running_loop().time()
            if text != previous:
                previous, changed = text, now
            if not pending and now - changed >= 0.4:
                return True, idle, True, False
            if now - started >= 3:
                return True, idle, now - changed >= 0.4, bool(pending)
            await asyncio.sleep(0.1)

    @staticmethod
    async def _download(download, options):
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

    async def _acquire(self, url, options, state):
        browser = self.browser
        # One ephemeral context per document. Cookies, workers, POST/XHR and
        # redirects work natively inside it, but never survive another acquisition.
        context = await browser.new_context(accept_downloads=True, ignore_https_errors=False,
                                            no_viewport=self.headful)
        try:
            page = await context.new_page()
            pending = set()
            def requested(request):
                if request.resource_type in ('fetch', 'xhr'):
                    pending.add(request)
            def completed(request):
                pending.discard(request)
            page.on('request', requested)
            page.on('requestfinished', completed)
            page.on('requestfailed', completed)
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
            try:
                response = await page.goto(url, wait_until='domcontentloaded', timeout=min(10, options.request_timeout) * 1000)
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
                    return await self._download(await download_ready, options)
                if not state['challenge'] or not browser.is_connected():
                    raise
                response = navigation_response
            latest = lambda: navigation_response or response
            coverage = None
            while True:
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
                settled, idle, quiet, pending_load = await self._settle_rendered(page, latest, state, options, response, pending)
                if not settled:
                    continue
                if coverage is None:
                    # Reserve serialization/cleanup time. Page-controlled native
                    # expansion can replace the document: validate it again,
                    # including challenge continuation, HTTP errors and downloads,
                    # under the original deadline without expanding it again.
                    async def revalidate():
                        return await self._settled_response(page, latest, state) is response
                    coverage = await expand_content(page, options, deadline=state['deadline'] - 0.15,
                                                    revalidate=revalidate)
                    coverage['initial_render'] = {'network_idle': idle, 'dom_quiet': quiet,
                                                  'pending_native_load': pending_load,
                                                  'partial': not idle or not quiet or pending_load}
                if not idle or not quiet or pending_load:
                    coverage['partial'] = True
                    if coverage['stop_reason'] in ('settled', 'no_progress'):
                        coverage['stop_reason'] = 'render_settling_limit'
                if await self._settled_response(page, latest, state) is response:
                    break
            # Bound DOM serialization, not browser allocations/resource traffic.
            within = await page.evaluate(DOM_WITHIN_LIMIT, options.dom_bytes)
            if not within:
                raise FetchError('resource_limit')
            html = await page.content()
            final_url = page.url
            if len(html.encode()) > options.dom_bytes:
                raise FetchError('resource_limit')
            links = LinkTargets(final_url)
            links.feed(html)
            # Local Crawl4AI scraping/Markdown conversion only: no networking.
            config = CrawlerRunConfig(word_count_threshold=0)
            params = config.__dict__.copy()
            params.pop('url', None)
            scraped = config.scraping_strategy.scrap(final_url, main_content_html(html), **params)
            cleaned_html = scraped['cleaned_html'] if isinstance(scraped, dict) else scraped.cleaned_html
            markdown = DefaultMarkdownGenerator().generate_markdown(
                input_html=cleaned_html, base_url=links.base).raw_markdown
            if len(markdown.encode()) > options.response_bytes:
                raise FetchError('resource_limit')
            if not markdown.strip():
                raise FetchError('unsupported_format')
            return {'url': final_url, 'title': await page.title(), 'content': markdown,
                    'links': [urljoin(links.base, href) for href in links.targets],
                    'links_truncated': links.truncated, 'coverage': coverage}
        except BrowserTimeout:
            if state['challenge']:
                raise FetchError('blocked', reason='challenge_timeout',
                                 http_status=state['http_status']) from None
            raise FetchError('fetch_timeout') from None
        except Error:
            code = 'fetch_failed' if browser.is_connected() else 'browser_unavailable'
            raise FetchError(code) from None
        finally:
            with anyio.CancelScope(shield=True):
                await context.close()

    async def close(self):
        browser, self.browser = self.browser, None
        try:
            if browser is not None:
                try:
                    async with asyncio.timeout(2):
                        await browser.close()
                except Exception:
                    pass
        finally:
            if self.driver_pid is not None:
                try:
                    os.killpg(self.driver_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.driver_pid = None
            if self.driver_context is not None:
                context, self.driver_context = self.driver_context, None
                try:
                    async with asyncio.timeout(2):
                        await context.__aexit__()
                except Exception:
                    pass
            if self.profile is not None:
                self.profile.cleanup()
                self.profile = None
