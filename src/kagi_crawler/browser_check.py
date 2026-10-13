"""Opt-in browser/WebGL diagnostic; no service credentials or public navigation.

python -m kagi_crawler.browser_check --headful [--channel chrome]
Uses the service's native launch, secure DoH check and child environment filter.
Both probe contexts belong to a fresh owned profile, never a desktop profile.
No rendering flags are injected: null WebGL is an observation, not a reason to
silently opt untrusted pages into unsafe software-renderer settings.
"""
import argparse
import asyncio
from contextlib import nullcontext
import json
import os

from patchright.async_api import Error

from .browser import Browser
from .headful import virtual_display
from .transport import FetchError


_PROBE = '''() => {
    const graphics = kind => {
        const gl = document.createElement('canvas').getContext(kind);
        if (!gl) return {available: false, vendor: null, renderer: null};
        const extension = gl.getExtension('WEBGL_debug_renderer_info');
        const result = {available: true,
            vendor: extension ? gl.getParameter(extension.UNMASKED_VENDOR_WEBGL) : null,
            renderer: extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : null};
        gl.getExtension('WEBGL_lose_context')?.loseContext();
        return result;
    };
    return {user_agent: navigator.userAgent, webdriver: navigator.webdriver,
        viewport: {width: innerWidth, height: innerHeight},
        webgl: graphics('webgl'), webgl2: graphics('webgl2')};
}'''


async def check():
    owner = Browser()
    try:
        async with asyncio.timeout(30):
            await owner.start()
            report = {'channel': owner.channel, 'headful': owner.headful,
                      'browser_version': owner.browser.version, 'secure_doh_verified': True}
            # Compare the fresh persistent recipe with the service's isolated
            # acquisition contexts, without weakening production cookie isolation.
            persistent = owner.browser.contexts[0]
            ephemeral = await owner.browser.new_context(accept_downloads=True,
                ignore_https_errors=False, no_viewport=owner.headful)
            try:
                for name, context in (('persistent_profile', persistent),
                                      ('ephemeral_context', ephemeral)):
                    page = await context.new_page()
                    try:
                        report[name] = await page.evaluate(_PROBE)
                    finally:
                        await page.close()
            finally:
                await ephemeral.close()
            return report
    finally:
        await owner.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', choices=('chromium', 'chrome'))
    parser.add_argument('--headful', action='store_true')
    arguments = parser.parse_args()
    if arguments.channel:
        os.environ['KAGI_CRAWLER_BROWSER_CHANNEL'] = arguments.channel
    headed = arguments.headful or os.environ.get('KAGI_CRAWLER_HEADFUL') == '1'
    try:
        with virtual_display() if headed else nullcontext():
            report = asyncio.run(check())
    except (FetchError, Error, OSError, TimeoutError) as error:
        print(json.dumps({'error': {'code': error.code if isinstance(error, FetchError)
                                   else 'browser_unavailable'}}))
        raise SystemExit(1) from None
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
