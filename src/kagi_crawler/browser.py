"""Pinned Crawl4AI/Patchright rendering with no direct-network fallback.

HTTP fingerprints belong to aiohttp, not Chromium. This is best-effort stealth,
not a general Chromium sandbox: deploy with process memory limits and an egress
firewall too. QUIC and nonproxied WebRTC UDP are disabled; no engine exploit
or hard browser CPU/RAM containment is claimed.
"""
import asyncio
import os
import signal
from pathlib import Path
import sys
import tempfile
import uuid
from html.parser import HTMLParser
from urllib.parse import urljoin
import patchright
import anyio

# Patchright exposes a node executable override, but no driver environment field.
# Serialize only startup while installing an exec wrapper: the actual driver and
# all its children receive an allowlisted environment, not discovery/access secrets.
_DRIVER_START_LOCK = asyncio.Lock()
_RUNTIME_ENV = ('PATH', 'HOME', 'TMPDIR', 'LD_LIBRARY_PATH', 'DISPLAY', 'LANG',
                'PLAYWRIGHT_BROWSERS_PATH')


from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
from crawl4ai.browser_adapter import UndetectedAdapter
from crawl4ai.browser_manager import BrowserManager
from crawl4ai.async_configs import ProxyConfig

from .transport import FetchError


class IsolatedBrowserManager(BrowserManager):
    def _build_browser_args(self):
        # Crawl4AI 0.9.4 lacks a public browser-env field. Keep this small pinned
        # integration seam until upstream adds one; never inherit service secrets
        # into Chromium. No persistent/private profile is used.
        args = super()._build_browser_args()
        # Upstream hardcodes certificate-bypass flags despite BrowserConfig;
        # never carry those into this untrusted-page browser.
        args['args'] = [a for a in args['args'] if not a.startswith('--ignore-certificate-errors')]
        args['env'] = {k: os.environ[k] for k in _RUNTIME_ENV if k in os.environ}
        return args


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
        self.crawler = None
        self.proxy = None
        self.proxy_tasks = set()
        self.start_lock = asyncio.Lock()
        self.denied = 0
        self.driver_pid = None
        self.driver_context = None

    async def _deny(self, reader, writer):
        task = asyncio.current_task()
        self.proxy_tasks.add(task)
        self.denied += 1
        try:
            writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.proxy_tasks.discard(task)

    async def start(self):
        async with self.start_lock:
            if self.crawler is not None:
                if not self.crawler.crawler_strategy.browser_manager.browser.is_connected():
                    raise FetchError('browser_unavailable')
                return
            self.proxy = await asyncio.start_server(self._deny, '127.0.0.1', 0)
            port = self.proxy.sockets[0].getsockname()[1]
            self.proxy_config = ProxyConfig(server=f'http://127.0.0.1:{port}')
            config = BrowserConfig(browser_type='chromium', headless=True,
                use_persistent_context=False, create_isolated_context=True,
                ignore_https_errors=False, accept_downloads=False, verbose=False,
                proxy_config=self.proxy_config, extra_args=[
                    '--proxy-bypass-list=<-loopback>', '--disable-quic',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                    '--disable-background-networking', '--disable-component-update',
                    '--disable-sync', '--disable-extensions', '--disable-features=ServiceWorker'])
            strategy = AsyncPlaywrightCrawlerStrategy(browser_config=config,
                                                      browser_adapter=UndetectedAdapter())
            strategy.browser_manager = IsolatedBrowserManager(config, logger=strategy.logger,
                                                               use_undetected=True)
            self.crawler = AsyncWebCrawler(crawler_strategy=strategy, config=config)
            try:
                async with _DRIVER_START_LOCK:
                    node = os.environ.get('PLAYWRIGHT_NODEJS_PATH') or str(Path(patchright.__file__).parent / 'driver' / 'node')
                    safe_env = {k: os.environ[k] for k in _RUNTIME_ENV if k in os.environ}
                    with tempfile.TemporaryDirectory(prefix='crawler-driver-') as directory:
                        wrapper = Path(directory) / 'node'
                        pidfile = Path(directory) / 'pid'
                        # Own a process group even before Patchright completes its
                        # handshake. A cancelled/failed startup may not have a
                        # Playwright handle capable of closing its driver yet.
                        wrapper.write_text(f'#!{sys.executable}\nimport os, sys\nos.setsid()\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\nos.execve({node!r}, [{node!r}, *sys.argv[1:]], {safe_env!r})\n')
                        wrapper.chmod(0o700)
                        previous = os.environ.get('PLAYWRIGHT_NODEJS_PATH')
                        os.environ['PLAYWRIGHT_NODEJS_PATH'] = str(wrapper)
                        import patchright.async_api as driver_api
                        factory = driver_api.async_playwright
                        def owned_driver():
                            self.driver_context = factory()
                            return self.driver_context
                        # Pinned Crawl4AI drops this manager if the handshake is
                        # cancelled. Keep ownership even before start() returns
                        # the public Playwright handle. Startup is serialized.
                        driver_api.async_playwright = owned_driver
                        try:
                            await self.crawler.start()
                        finally:
                            driver_api.async_playwright = factory
                            if self.driver_context is not None and not pidfile.exists():
                                # Cancellation can arrive between spawning the
                                # wrapper and its first instruction. Let ownership
                                # handshake finish before removing the directory.
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

    async def render(self, initial, transport):
        await self.start()
        denied_before = self.denied
        session_id = str(uuid.uuid4())
        contexts = set()
        tasks = set()
        errors = []
        staged = True

        async def hook(page, context, **_):
            contexts.add(context)

            async def route(handler):
                nonlocal staged
                task = asyncio.current_task()
                tasks.add(task)
                try:
                    request = handler.request
                    if request.method not in ('GET', 'HEAD'):
                        raise FetchError('rendering_unsupported')
                    transport.policy.url(request.url)
                    if request.resource_type in ('image', 'media', 'font'):
                        await handler.abort()
                        return
                    if staged and request.is_navigation_request() and request.url == initial.url:
                        staged = False
                        response = initial
                    else:
                        if request.is_navigation_request() and request.frame.parent_frame is None:
                            raise FetchError('rendering_unsupported')
                        # Fulfilled 3xx redirects aren't necessarily re-intercepted.
                        # Fail explicitly instead of silently changing origin semantics.
                        response = await transport.fetch(request.url, redirects=False, method=request.method)
                    await handler.fulfill(status=response.status,
                        headers=response.fulfill_headers(), body=response.body)
                except FetchError as error:
                    errors.append(error.code)
                    await handler.abort()
                except (asyncio.TimeoutError, ConnectionError, OSError):
                    errors.append('fetch_timeout')
                    await handler.abort()
                except Exception:
                    errors.append('fetch_failed')
                    await handler.abort()
                finally:
                    tasks.discard(task)

            async def websocket(ws):
                errors.append('rendering_unsupported')
                await ws.close()

            await context.route('**/*', route)
            await context.route_web_socket('**/*', websocket)
            return page

        async def bound_dom(page, **_):
            # Bound serialization before page.content() crosses the driver IPC.
            # This does not bound allocations made by hostile JS beforehand;
            # deployment process/container memory limits remain required.
            session = await page.context.new_cdp_session(page)
            try:
                await session.send('Emulation.setScriptExecutionDisabled', {'value': True})
                within = await page.evaluate('''limit => {
                    let total = 0, count = 0;
                    const walk = document.createTreeWalker(document, NodeFilter.SHOW_ALL);
                    while (walk.nextNode()) {
                        const node = walk.currentNode;
                        if (++count > 50000) return false;
                        total += 32 + (node.nodeValue || '').length * 6;
                        for (const attr of node.attributes || []) total += (attr.name.length + attr.value.length) * 6 + 8;
                        if (total > limit) return false;
                    }
                    return true;
                }''', transport.options.response_bytes)
                if not within:
                    errors.append('resource_limit')
                    raise FetchError('resource_limit')
            finally:
                await session.detach()
            return page

        self.crawler.crawler_strategy.set_hook('on_page_context_created', hook)
        self.crawler.crawler_strategy.set_hook('before_retrieve_html', bound_dom)
        try:
            result = await self.crawler.arun(initial.url, config=CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS, session_id=session_id,
                proxy_config=self.proxy_config, page_timeout=int(transport.options.request_timeout * 1000),
                wait_until='networkidle', delay_before_return_html=0,
                word_count_threshold=0, verbose=False, log_console=False,
                max_retries=0, fallback_fetch_function=None, check_robots_txt=False,
                screenshot=False, capture_network_requests=False, capture_console_messages=False))
            if errors:
                raise FetchError(errors[0])
            if self.denied != denied_before:
                raise FetchError('rendering_unsupported')
            if not result.success:
                if not self.crawler.crawler_strategy.browser_manager.browser.is_connected():
                    raise FetchError('browser_unavailable')
                code = 'blocked' if (result.error_message or '').startswith('Blocked by anti-bot protection:') else 'fetch_failed'
                raise FetchError(code)
            if len(result.html.encode()) > transport.options.response_bytes:
                raise FetchError('resource_limit')
            markdown = result.markdown.raw_markdown
            if not markdown.strip():
                raise FetchError('unsupported_format')
            if len(markdown.encode()) > transport.options.response_bytes:
                raise FetchError('resource_limit')
            # Canonical cached documents retain bounded original link targets,
            # independently of query evidence. Parse already-bounded rendered HTML
            # because upstream link metadata normalizes away meaningful query keys.
            links = LinkTargets(initial.url)
            links.feed(result.html)
            targets = [urljoin(links.base, href) for href in links.targets]
            return result.metadata.get('title', ''), markdown, targets, links.truncated
        finally:
            # Explicit session release closes pages/context (including popups and
            # workers), unlike Crawl4AI's last-headless-page retention. Cancelling
            # a MCP call must not leave route downloads alive.
            with anyio.CancelScope(shield=True):
                for task in list(tasks):
                    if task is not asyncio.current_task():
                        task.cancel()
                await asyncio.gather(*list(tasks), return_exceptions=True)
                await self.crawler.crawler_strategy.browser_manager.kill_session(session_id)
                for context in contexts:
                    await context.close()

    async def close(self):
        crawler, self.crawler = self.crawler, None
        try:
            if crawler is not None:
                try:
                    async with asyncio.timeout(2):
                        await crawler.close()
                except Exception:
                    # Teardown must not hang forever on a crashed driver. The
                    # owned group below is the final cancellation/startup guard.
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
                # Termination above unblocks even a driver that never answered
                # initialization. Reap its transport and pending init callbacks.
                try:
                    async with asyncio.timeout(2):
                        await context.__aexit__()
                except Exception:
                    pass
            if self.proxy is not None:
                self.proxy.close()
                await self.proxy.wait_closed()
                self.proxy = None
            for task in list(self.proxy_tasks):
                task.cancel()
            await asyncio.gather(*list(self.proxy_tasks), return_exceptions=True)
