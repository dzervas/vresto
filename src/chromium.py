"""Chrome-specific bootstrap, not a portable browser-engine contract.

Pinned Chrome's Local State DoH seed, managed-policy-aware settings WebUI/CDP
probe, external-PDF preference and Patchright driver process-group ownership
belong here. A Firefox/Playwright engine would need its own bootstrap, not these
assumptions. Acquisition and document rendering do not configure the engine.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from typing import cast

import anyio
from patchright.async_api import Browser, BrowserContext, Page
import patchright

from .acquisition_policy import FetchError

_DRIVER_START_LOCK = asyncio.Lock()
_RUNTIME_ENV = ('PATH', 'HOME', 'TMPDIR', 'LD_LIBRARY_PATH', 'DISPLAY', 'LANG',
                'PLAYWRIGHT_BROWSERS_PATH', 'FONTCONFIG_FILE', 'FONTCONFIG_PATH')


class Chromium:
    def __init__(self) -> None:
        self.headful = os.environ.get('VRESTO_HEADFUL') == '1'
        self.channel = os.environ.get('VRESTO_BROWSER_CHANNEL', 'chromium')
        if self.channel not in ('chromium', 'chrome'):
            raise ValueError('browser channel must be chromium or chrome')
        self.browser = None
        self.context = None
        self.profile = None
        self.driver_pid = None
        self.driver_context = None
        self.page_lock = asyncio.Lock()

    async def new_page(self) -> Page:
        context = cast(BrowserContext, self.context)
        # Focus emulation does not keep inactive tabs painting after layout
        # changes (even in headless Chrome). Give each acquisition its own window,
        # without losing the shared profile's cookies/cache. Serialize creation
        # only, so expect_page cannot hand concurrent callers the same target;
        # navigation, rendering and native input still run concurrently.
        async with self.page_lock:
            session = await cast(Browser, self.browser).new_browser_cdp_session()
            try:
                async with context.expect_page() as opened:
                    await session.send('Target.createTarget', {'url': 'about:blank', 'newWindow': True})
                return await opened.value
            finally:
                await session.detach()

    async def start(self) -> None:
        # Pinned Chrome 153 default_dns_over_https_config_source.cc reads
        # browser-wide Local State, not profile prefs or invented DoH flags.
        # Full Chrome (not headless shell) provides this configuration seam.
        # Acquisition tabs share this owned profile, including cookies, storage,
        # workers and clearance state; retirement deletes it, never the user profile.
        self.profile = tempfile.TemporaryDirectory(prefix='crawler-chrome-')
        Path(self.profile.name, 'Local State').write_text(json.dumps({
            'dns_over_https': {'mode': 'secure', 'templates': 'https://1.1.1.1/dns-query'}}))
        Path(self.profile.name, 'Default').mkdir()
        Path(self.profile.name, 'Default', 'Preferences').write_text(json.dumps({
            'plugins': {'always_open_pdf_externally': True}}))
        async with _DRIVER_START_LOCK:
            node = os.environ.get('PLAYWRIGHT_NODEJS_PATH') or str(Path(patchright.__file__).parent / 'driver' / 'node')
            safe_env: dict[str, str | float | bool] = {k: os.environ[k] for k in _RUNTIME_ENV if k in os.environ}
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
                        # Keep Patchright's fixed 1280×720 viewport so responsive
                        # content and scrolling do not depend on headed window
                        # decorations or the virtual display's screen size.
                        env=safe_env, ignore_https_errors=False,
                        accept_downloads=True,
                        # Images add traffic/layout work without public text; retain
                        # Patchright defaults and bound the shared disk cache to 256 MiB.
                        # Under Xvfb, cold GPU startup can withhold animation frames
                        # for ~1s even in visible/focused tabs. Native click stability
                        # needs those frames within 500ms; use software rendering in
                        # headed mode rather than force clicks or serialize tab focus.
                        args=['--blink-settings=imagesEnabled=false', '--disk-cache-size=268435456',
                              *(['--disable-gpu'] if self.headful else [])])
                    # Keep Patchright's own defaults: adding back
                    # --disable-component-update contradicts its patches.
                    self.context = bootstrap
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

    async def close(self) -> None:
        browser, self.browser = self.browser, None
        self.context = None
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
