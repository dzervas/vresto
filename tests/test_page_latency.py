import asyncio
from dataclasses import replace

from aiohttp import web
from patchright.async_api import Page
import pytest

from kagi_crawler.acquisition_policy import AcquisitionOptions, FetchError
from kagi_crawler.browser import Browser
from test_fetch import site


async def test_images_disabled_and_native_asset_cache_shared_until_retirement():
    requests = []
    async def handler(request):
        requests.append(request.path)
        if request.path == '/asset.js':
            return web.Response(text='document.body.dataset.cached = "yes";',
                                content_type='application/javascript',
                                headers={'Cache-Control': 'public, max-age=3600'})
        if request.path == '/image.png':
            return web.Response(body=b'image', content_type='image/png')
        return web.Response(content_type='text/html', text='''<h1>Cache fixture</h1>
            <img src="/image.png" alt="Public diagram"><script src="/asset.js"></script>''')
    async with site(handler) as (base, options):
        browser = Browser()
        try:
            for path in ['/first', '/second']:
                result = await browser.acquire(base + path, options)
                assert 'Cache fixture' in result['content']
                assert browser.runtime.context is not None
                # Closing an acquisition must not close the persistent context.
                assert len(browser.runtime.context.pages) == 1  # bootstrap blank tab
            assert requests.count('/asset.js') == 1
            assert '/image.png' not in requests
            await browser.close()
            await browser.acquire(base + '/restarted', options)
            assert requests.count('/asset.js') == 2
            assert '/image.png' not in requests
        finally:
            await browser.close()


async def test_long_lived_untracked_request_does_not_delay_quiet_dom(monkeypatch):
    opened, release, idle_drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    idle_cancelled = asyncio.Event()
    async def handler(request):
        if request.path == '/events':
            response = web.StreamResponse(headers={'Content-Type': 'application/javascript'})
            await response.prepare(request)
            await response.write(b'/* pending script')
            opened.set()
            await release.wait()
            return response
        return web.Response(content_type='text/html', text='''<h1>Quiet public evidence</h1>
            <script async src="/events"></script>''')
    wait = Page.wait_for_load_state
    async def observed_wait(self, state=None, **kwargs):
        if state != 'networkidle':
            return await wait(self, state, **kwargs)
        # Keep the real resource wait alive even on a slow test host, so this
        # tests quiet-DOM cancellation rather than racing the 1 s idle timeout.
        kwargs['timeout'] = 10000
        try:
            return await wait(self, state, **kwargs)
        except asyncio.CancelledError:
            idle_cancelled.set()
            raise
        finally:
            idle_drained.set()
    monkeypatch.setattr(Page, 'wait_for_load_state', observed_wait)
    async with site(handler) as (base, options):
        browser = Browser()
        try:
            result = await browser.acquire(base, options)
            assert opened.is_set() and idle_drained.is_set() and idle_cancelled.is_set()
            initial = result['coverage']['initial_render']
            assert initial == {'network_idle': False, 'dom_quiet': True,
                               'pending_native_load': False, 'partial': False}
            assert result['coverage']['stop_reason'] != 'render_settling_limit'
        finally:
            release.set()
            await browser.close()


@pytest.mark.parametrize('outcome', ['quiet', 'redirect', 'limit', 'cancel', 'pending'])
async def test_settle_drains_idle_task_on_every_exit(monkeypatch, outcome):
    started, drained = asyncio.Event(), asyncio.Event()
    response = object()
    class PageFixture:
        async def wait_for_load_state(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        async def evaluate(self, expression, *args):
            await started.wait()
            return outcome != 'limit' if args else 'unchanged'
    browser = Browser()
    async def validate(*args):
        await started.wait()
        return object() if outcome == 'redirect' else response
    monkeypatch.setattr(browser, '_settled_response', validate)
    task = asyncio.create_task(browser._settle_rendered(
        PageFixture(), None, {}, replace(AcquisitionOptions(), request_timeout=5),
        response, {'tracked'} if outcome == 'pending' else set()))
    await started.wait()
    if outcome == 'cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == 'limit':
        with pytest.raises(FetchError):
            await task
    else:
        result = await task
        assert result == (outcome != 'redirect', False, outcome != 'redirect', outcome == 'pending')
    assert drained.is_set()
