"""Origin-aware admission and browser-generation ownership, plus native equality."""
import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from vresto.acquisition import Acquisition
from vresto.acquisition_policy import AcquisitionOptions, FetchError
from vresto.cache import CachedAcquisition, DocumentCache
from test_acquisition_diagnostics import FakeBrowser
from test_fetch import site


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0)


def document(url):
    return {'url': url, 'title': url, 'content': url,
            'links': [], 'links_truncated': False, 'coverage': {}}


@pytest.mark.parametrize('value', [0, 9, True, 1.5])
def test_concurrency_validation(value):
    with pytest.raises(ValueError, match='browser concurrency'):
        AcquisitionOptions(browser_concurrency=value)


@pytest.mark.parametrize('cached', [False, True])
async def test_parallel_limit_origin_waiters_do_not_hold_slots_and_order(tmp_path, cached):
    browser = FakeBrowser()
    entered, completed = [], []
    gates = {url: asyncio.Event() for url in [
        'https://a.example/1', 'https://a.example/2', 'https://b.example/', 'https://c.example/']}

    async def acquire(url, options):
        entered.append(url)
        await gates[url].wait()
        completed.append(url)
        return document(url)

    browser.acquire.side_effect = acquire
    manager = Acquisition(AcquisitionOptions(browser_concurrency=2), browser_factory=lambda: browser)
    service = CachedAcquisition(manager, DocumentCache(tmp_path)) if cached else manager
    urls = list(gates)
    task = asyncio.create_task(service.fetch_many(urls))
    try:
        await until(lambda: len(entered) == 2)
        assert entered == [urls[0], urls[2]]
        assert manager.active == 2 and manager.waiting == 2
        gates[urls[2]].set()
        await until(lambda: len(entered) == 3)
        assert entered[-1] == urls[3]  # A's waiter cannot steal B's freed slot.
        gates[urls[3]].set()
        gates[urls[0]].set()
        await until(lambda: len(entered) == 4)
        gates[urls[1]].set()
        result = await task
        assert [s['requested_url'] for s in result['sources']] == urls
        assert [s['content'] for s in result['sources']] == urls
        assert completed != urls
        assert manager.active == manager.waiting == 0
    finally:
        await manager.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_same_origin_includes_scheme_and_effective_port():
    assert Acquisition._origin('https://example.org/a') == Acquisition._origin('https://example.org:443/b')
    assert Acquisition._origin('http://example.org/') != Acquisition._origin('https://example.org/')
    assert Acquisition._origin('https://example.org:80/') != Acquisition._origin('https://example.org/')


@pytest.mark.parametrize('startup', [False, True])
async def test_cancel_one_request_does_not_cancel_peer_or_shared_startup(startup):
    browser = FakeBrowser()
    release_start, release_page = asyncio.Event(), asyncio.Event()
    entered, cleaned = set(), set()

    async def start():
        await release_start.wait()

    async def acquire(url, options):
        entered.add(url)
        try:
            await release_page.wait()
            return document(url)
        finally:
            cleaned.add(url)

    browser.start.side_effect = start
    browser.acquire.side_effect = acquire
    manager = Acquisition(browser_factory=lambda: browser)
    urls = ['https://a.example/', 'https://b.example/']
    first, second = [asyncio.create_task(manager.fetch_many([url])) for url in urls]
    try:
        if startup:
            await until(lambda: manager.active == 2 and browser.start.await_count == 1)
        else:
            release_start.set()
            await until(lambda: len(entered) == 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        browser.close.assert_not_awaited()
        if not startup:
            assert cleaned == {urls[0]}
        release_start.set()
        release_page.set()
        assert (await second)['sources'][0]['content'] == urls[1]
        browser.start.assert_awaited_once()
        assert manager.active == manager.waiting == 0
    finally:
        await manager.close()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_crash_wakes_hung_peers_retires_once_and_next_request_recovers():
    browser, replacement = FakeBrowser(), FakeBrowser()
    browsers = iter([browser, replacement])
    entered, cleaned = set(), set()
    crash = asyncio.Event()

    async def acquire(url, options):
        entered.add(url)
        try:
            if url.startswith('https://a.'):
                await crash.wait()
                browser.connected = False
                raise FetchError('browser_unavailable')
            await asyncio.Event().wait()  # Even an adapter that never reports disconnect must drain.
        finally:
            cleaned.add(url)

    browser.acquire.side_effect = acquire
    replacement.acquire.side_effect = lambda url, options: document(url)
    manager = Acquisition(browser_factory=lambda: next(browsers))
    urls = ['https://a.example/', 'https://b.example/', 'https://c.example/']
    task = asyncio.create_task(manager.fetch_many(urls))
    try:
        await until(lambda: len(entered) == 3)
        crash.set()
        result = await asyncio.wait_for(task, 2)
        assert [s['error']['code'] for s in result['sources']] == ['browser_unavailable'] * 3
        assert cleaned == set(urls)
        browser.close.assert_awaited_once()
        assert manager.active == 0
        result = await manager.fetch_many(['https://d.example/'])
        assert result['sources'][0]['content'] == 'https://d.example/'
        assert manager.browser is replacement
        # A late failure from an old generation must not close its replacement.
        await manager._retire('browser_unavailable', expected=browser)
        replacement.close.assert_not_awaited()
    finally:
        await manager.close()


async def test_connected_tab_failure_does_not_retire_peer():
    browser = FakeBrowser()
    release = asyncio.Event()
    entered = set()

    async def acquire(url, options):
        entered.add(url)
        await release.wait()
        if url.startswith('https://a.'):
            raise RuntimeError('tab conversion failure')
        return document(url)

    browser.acquire.side_effect = acquire
    manager = Acquisition(browser_factory=lambda: browser)
    task = asyncio.create_task(manager.fetch_many(['https://a.example/', 'https://b.example/']))
    try:
        await until(lambda: len(entered) == 2)
        release.set()
        result = await task
        assert result['sources'][0]['error']['code'] == 'fetch_failed'
        assert result['sources'][1]['content'] == 'https://b.example/'
        browser.close.assert_not_awaited()
    finally:
        await manager.close()


async def test_close_drains_multiple_pages_and_origin_waiters():
    browser = FakeBrowser()
    entered, cleaned = set(), set()

    async def acquire(url, options):
        entered.add(url)
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.add(url)

    browser.acquire.side_effect = acquire
    manager = Acquisition(browser_factory=lambda: browser)
    tasks = [asyncio.create_task(manager.fetch_many([url])) for url in
             ['https://a.example/1', 'https://b.example/', 'https://a.example/2']]
    await until(lambda: len(entered) == 2 and manager.waiting == 1)
    await asyncio.wait_for(manager.close(), 2)
    await asyncio.gather(*tasks, return_exceptions=True)
    assert cleaned == entered
    assert manager.active == manager.waiting == 0
    assert not manager.tasks and not manager.origins
    browser.close.assert_awaited_once()
    assert (await manager.fetch_many(['https://a.example/']))['sources'][0]['error']['code'] == 'fetch_failed'


async def test_origin_queue_deadline_and_backoff_are_shared():
    browser = FakeBrowser()
    entered = asyncio.Event()

    async def acquire(url, options):
        entered.set()
        await asyncio.Event().wait()

    browser.acquire.side_effect = acquire
    manager = Acquisition(AcquisitionOptions(deadline=.1), browser_factory=lambda: browser)
    task = asyncio.create_task(manager.fetch_many(['https://a.example/1', 'https://a.example/2']))
    try:
        await entered.wait()
        result = await task
        assert [s['error']['code'] for s in result['sources']] == ['fetch_timeout', 'fetch_timeout']
        browser.acquire.assert_awaited_once()
        browser.acquire.side_effect = FetchError('rate_limited')
        result = await manager.fetch_many(['https://b.example/1', 'https://b.example:443/2'])
        assert [s['error']['code'] for s in result['sources']] == ['rate_limited', 'rate_limited']
        assert browser.acquire.await_count == 2
    finally:
        await manager.close()


async def test_pdf_parsers_are_separately_serialized(monkeypatch):
    browser = FakeBrowser()
    browser.acquire.side_effect = lambda url, options: {'url': url, 'pdf': b'%PDF-'}
    running = 0
    peak = 0

    async def parse(data, options):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(.02)
        running -= 1
        return 'title', 'text', {}

    parser = AsyncMock(side_effect=parse)
    monkeypatch.setattr('vresto.acquisition.parse_pdf', parser)
    manager = Acquisition(browser_factory=lambda: browser)
    try:
        result = await manager.fetch_many([f'https://{i}.example/' for i in range(4)])
        assert all(s['document_type'] == 'pdf' for s in result['sources'])
        assert parser.await_count == 4 and peak == 1
    finally:
        await manager.close()


async def test_cached_hit_bypasses_full_admission_and_misses_share_deadline(tmp_path):
    browser = FakeBrowser()
    browser.acquire.side_effect = lambda url, options: document(url)
    manager = Acquisition(AcquisitionOptions(browser_concurrency=1, deadline=.12),
                          browser_factory=lambda: browser)
    cached = CachedAcquisition(manager, DocumentCache(tmp_path))
    try:
        await cached.fetch_many(['https://hit.example/'])
        entered = asyncio.Event()

        async def acquire(url, options):
            entered.set()
            await asyncio.Event().wait()

        browser.acquire.side_effect = acquire
        busy = asyncio.create_task(cached.fetch_many(['https://busy.example/']))
        await entered.wait()
        hit = await cached.fetch_many(['https://hit.example/'])
        assert hit['sources'][0]['cache_state'] == 'hit'
        result = await cached.fetch_many(['https://a.example/', 'https://b.example/'])
        assert [s['error']['code'] for s in result['sources']] == ['fetch_timeout'] * 2
        await busy
        assert manager.active == manager.waiting == 0
    finally:
        await manager.close()


@pytest.mark.parametrize('headed', [False, pytest.param(True, marks=pytest.mark.skipif(
    not os.environ.get('DISPLAY'), reason='headed Chrome requires DISPLAY'))])
async def test_native_content_equal_for_concurrency_one_and_four(tmp_path, monkeypatch, headed):
    # Cover cold GPU startup and post-detail layout changes: inactive tabs can
    # lose animation frames needed by native click's 500ms stability check.
    monkeypatch.setenv('VRESTO_HEADFUL', '1' if headed else '0')
    html = '''<main style="min-height:2400px"><h1>Concurrent fixture</h1><p>Stable public content.</p>
    <details ontoggle="if(this.open && !this.dataset.loaded) {this.dataset.loaded='1';this.insertAdjacentHTML('beforeend','<p>Native detail event.</p>');}">
    <summary>More</summary><p>Expanded continuation.</p><pre>Public code example.</pre></details>
    <div id="items">Initial item.</div><button type="button" aria-controls="items" onclick="document.getElementById('items').append(' Loaded item.');this.remove()">Load more</button></main>
    <script>let loaded=false; window.addEventListener('scroll', () => {
    if (loaded) return; loaded=true;
    setTimeout(() => document.querySelector('main').insertAdjacentHTML('beforeend',
    '<p>Lazy discussion continuation.</p>'), 300);
    });
    const viewport=document.createElement('p');
    viewport.textContent='Viewport '+innerWidth+'x'+innerHeight;
    document.querySelector('main').append(viewport);</script>'''

    async def handler(request):
        return web.Response(text=html, content_type='text/html')

    async with AsyncExitStack() as stack:
        sites = [await stack.enter_async_context(site(handler)) for _ in range(4)]
        urls = [base for base, _ in sites]
        destinations = tuple(d for _, options in sites for d in options.fixture_destinations)
        results = []
        for n in (1, 4):
            manager = Acquisition(replace(sites[0][1], browser_concurrency=n,
                                          fixture_destinations=destinations))
            cached = CachedAcquisition(manager, DocumentCache(tmp_path / str(n)))
            try:
                results.append((await cached.fetch_many(urls))['sources'])
            finally:
                await manager.close()
        for before, after in zip(*results):
            assert 'error' not in before and 'error' not in after
            assert before['content'] == after['content']
            assert before['content_version'] == after['content_version']
            for source in (before, after):
                assert 'Expanded continuation.' in source['content']
                assert 'Native detail event.' in source['content']
                assert 'Loaded item.' in source['content']
                assert 'Lazy discussion continuation.' in source['content']
                assert 'Viewport 1280x720' in source['content']
                assert source['coverage']['actions'] >= 2
                assert source['coverage']['stop_reason'] != 'control_unavailable'


async def test_idle_waits_for_last_of_multiple_completions():
    browser = FakeBrowser()
    now = [0.0]
    wake = asyncio.Event()
    entered = set()
    gates = {url: asyncio.Event() for url in ['https://a.example/', 'https://b.example/']}

    async def sleep(delay):
        await wake.wait()
        wake.clear()

    async def acquire(url, options):
        entered.add(url)
        await gates[url].wait()
        return document(url)

    browser.acquire.side_effect = acquire
    manager = Acquisition(clock=lambda: now[0], sleep=sleep, browser_factory=lambda: browser)
    task = asyncio.create_task(manager.fetch_many(list(gates)))
    try:
        await until(lambda: len(entered) == 2)
        idle = manager.idle_task
        now[0] = 3600
        wake.set()
        gates['https://a.example/'].set()
        await until(lambda: manager.active == 1)
        assert manager.last_work == 3600
        now[0] = 7200
        browser.close.assert_not_awaited()
        gates['https://b.example/'].set()
        await task
        assert manager.last_work == 7200
        browser.close.assert_not_awaited()
        now[0] = 10800
        wake.set()
        await idle
        browser.close.assert_awaited_once()
    finally:
        await manager.close()


async def test_new_request_racing_idle_teardown_uses_replacement():
    browser, replacement = FakeBrowser(), FakeBrowser()
    browsers = iter([browser, replacement])
    now = [0.0]
    wake, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        await wake.wait()
        wake.clear()

    async def close():
        closing.set()
        await release_close.wait()
        browser.connected = False

    browser.close.side_effect = close
    replacement.acquire.side_effect = lambda url, options: document(url)
    manager = Acquisition(AcquisitionOptions(deadline=.05), clock=lambda: now[0],
                          sleep=sleep, browser_factory=lambda: next(browsers))
    try:
        await manager.warmup()
        old_idle = manager.idle_task
        now[0] = 3600
        wake.set()
        await closing.wait()
        timed = asyncio.create_task(manager.fetch_many(['https://timeout.example/']))
        await until(lambda: manager.waiting == 1)
        assert (await timed)['sources'][0]['error']['code'] == 'fetch_timeout'
        request = asyncio.create_task(manager.fetch_many(['https://a.example/']))
        # ASGI's eager warmup may also race teardown, outside admission.
        startup = manager.warmup()
        await asyncio.sleep(0)
        assert manager.active == 0
        release_close.set()
        await old_idle
        await startup
        assert (await request)['sources'][0]['content'] == 'https://a.example/'
        assert manager.browser is replacement
        assert manager.idle_task is not old_idle and not manager.idle_task.done()
        browser.close.assert_awaited_once()
        replacement.close.assert_not_awaited()
    finally:
        release_close.set()
        await manager.close()


async def test_crash_also_drains_active_and_waiting_pdf_parsers(monkeypatch):
    browser = FakeBrowser()
    parsed, cleaned, crash = asyncio.Event(), asyncio.Event(), asyncio.Event()
    downloaded = set()

    async def acquire(url, options):
        if url.startswith('https://crash.'):
            await crash.wait()
            browser.connected = False
            raise FetchError('browser_unavailable')
        downloaded.add(url)
        return {'url': url, 'pdf': b'%PDF-'}

    async def parse(data, options):
        parsed.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    browser.acquire.side_effect = acquire
    parser = AsyncMock(side_effect=parse)
    monkeypatch.setattr('vresto.acquisition.parse_pdf', parser)
    manager = Acquisition(browser_factory=lambda: browser)
    task = asyncio.create_task(manager.fetch_many([
        'https://a.example/', 'https://b.example/', 'https://crash.example/']))
    try:
        await parsed.wait()
        await until(lambda: len(downloaded) == 2)
        crash.set()
        result = await asyncio.wait_for(task, 2)
        assert [s['error']['code'] for s in result['sources']] == ['browser_unavailable'] * 3
        assert cleaned.is_set() and not manager.pdf_lock.locked()
        parser.assert_awaited_once()
        browser.close.assert_awaited_once()
        assert manager.active == manager.waiting == 0
    finally:
        await manager.close()


@pytest.mark.parametrize('headed', [False, True])
async def test_bootstrap_keeps_default_per_tab_viewport_and_headed_software_rendering(monkeypatch, headed):
    from types import SimpleNamespace
    from patchright import async_api
    from vresto.chromium import Chromium

    launch = AsyncMock(side_effect=RuntimeError('stop before native launch'))
    driver = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch))), stop=AsyncMock())
    monkeypatch.setattr(async_api, 'async_playwright', lambda: driver)
    runtime = Chromium()
    runtime.headful = headed
    try:
        with pytest.raises(RuntimeError, match='stop before native launch'):
            await runtime.start()
        options = launch.call_args.kwargs
        assert options['headless'] is not headed
        assert ('--disable-gpu' in options['args']) is headed
        # Omission selects Patchright's 1280×720 default in every window,
        # independent of the headed display's native window geometry.
        assert options.get('no_viewport', False) is False
        assert 'viewport' not in options
    finally:
        await runtime.close()
