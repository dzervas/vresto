import asyncio
import os
import sys
from dataclasses import replace

import httpx
import psutil
import pytest

from test_http import running, client, body
from test_fetch import browser_pids, site
from aiohttp import web


async def eventually(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_initialization_warms_one_browser_without_acquisition_and_shutdown_reaps_it(tmp_path):
    before = browser_pids()
    async with running(tmp_path) as (url, _):
        async with client(url) as first, client(url) as second:
            await eventually(lambda: bool(browser_pids() - before))
            warm = browser_pids() - before
            assert len(warm) == 1
            await first.list_tools()
            await second.call_tool('search', {'query': 'docs', 'content_mode': 'discovery'})
            assert browser_pids() - before == warm
    await eventually(lambda: browser_pids() == before)


@pytest.mark.parametrize('complete', [True, False])
async def test_slow_startup_is_coalesced_nonblocking_and_shutdown_reaps_driver(tmp_path, monkeypatch, complete):
    node = os.environ['PLAYWRIGHT_NODEJS_PATH']
    marker = tmp_path / 'driver-pids'
    release = tmp_path / 'release'
    wrapper = tmp_path / 'slow-node'
    wrapper.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
with open({str(marker)!r}, 'a') as out: out.write(str(os.getpid()) + '\\n')
while not Path({str(release)!r}).exists(): time.sleep(0.01)
os.execv({node!r}, [{node!r}, *sys.argv[1:]])
''')
    wrapper.chmod(0o700)
    monkeypatch.setenv('PLAYWRIGHT_NODEJS_PATH', str(wrapper))
    before = browser_pids()
    async with running(tmp_path) as (url, _):
        # Real HTTP initialize and discovery complete while the external driver
        # deliberately cannot answer its startup handshake.
        async with asyncio.timeout(2):
            async with client(url) as first, client(url) as second:
                await first.list_tools()
                data = body(await second.call_tool('search', {'query': 'docs', 'content_mode': 'discovery'}))
                assert len(data['results']) == 10
        await eventually(marker.exists)
        pids = [int(p) for p in marker.read_text().splitlines()]
        assert len(pids) == 1
        assert browser_pids() == before
        if complete:
            release.touch()
            await eventually(lambda: len(browser_pids() - before) == 1)
    await eventually(lambda: not any(psutil.pid_exists(p) for p in pids))
    await eventually(lambda: browser_pids() == before)


async def test_cancelled_startup_acquisition_preserves_shared_driver_and_recovers_same_session(tmp_path, monkeypatch):
    from mcp import types
    node = os.environ['PLAYWRIGHT_NODEJS_PATH']
    marker = tmp_path / 'driver-pid'
    release = tmp_path / 'release'
    wrapper = tmp_path / 'slow-node'
    wrapper.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
Path({str(marker)!r}).write_text(str(os.getpid()))
while not Path({str(release)!r}).exists(): time.sleep(0.01)
os.execv({node!r}, [{node!r}, *sys.argv[1:]])
''')
    wrapper.chmod(0o700)
    monkeypatch.setenv('PLAYWRIGHT_NODEJS_PATH', str(wrapper))
    received = asyncio.Event()
    async def handler(request):
        received.set()
        return web.Response(content_type='text/html', text='<h1>Recovered startup</h1><p>' + 'Public technical documentation. ' * 20 + '</p>')
    calls = []
    before = browser_pids()
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, calls=calls) as session:
        await eventually(marker.exists)
        pid = int(marker.read_text())
        task = asyncio.create_task(session.call_tool('fetch', {'urls': [base]}))
        await asyncio.sleep(.1)
        assert not received.is_set()  # Native acquisition waits for its driver.
        await session.send_notification(types.ClientNotification(types.CancelledNotification(
            method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=calls[-1]))))
        try:
            await task
        except Exception as exc:
            assert 'cancel' in str(exc).lower()
        assert psutil.pid_exists(pid)  # Shared warmup belongs to the service, not this request.
        release.touch()
        recovered = body(await session.call_tool('fetch', {'urls': [base]}))
        assert 'Recovered startup' in recovered['sources'][0]['content']
    await eventually(lambda: browser_pids() == before)


async def test_startup_failure_does_not_break_discovery_but_acquisition_reports_it(tmp_path, monkeypatch):
    wrapper = tmp_path / 'failed-node'
    wrapper.write_text(f'#!{sys.executable}\nraise SystemExit(1)\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv('PLAYWRIGHT_NODEJS_PATH', str(wrapper))
    before = browser_pids()
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Cannot render</h1>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        await session.list_tools()
        result = body(await session.call_tool('search', {'query': 'docs', 'content_mode': 'discovery'}))
        assert len(result['results']) == 10
        failed = body(await session.call_tool('fetch', {'urls': [base]}))
        assert failed['sources'][0]['error']['code'] == 'browser_unavailable'
    await eventually(lambda: browser_pids() == before)


async def test_one_request_deadline_bounds_all_urls_without_new_downloads_after_expiry(tmp_path):
    received = []
    async def handler(request):
        received.append(request.path)
        await asyncio.sleep(1.5)
        return web.Response(status=404)
    clock = Clock()
    async with site(handler) as (base, options):
        options = replace(options, deadline=2.5)
        async with running(tmp_path, acquisition_options=options, browser_clock=clock,
                           browser_sleep=clock.sleep) as (url, _), client(url) as session:
            await asyncio.wait_for(clock.ready.wait(), 10)
            data = body(await session.call_tool('fetch', {'urls': [base + '/first', base + '/second', base + '/third']}))
            assert [s['error']['code'] for s in data['sources']] == ['fetch_failed', 'fetch_timeout', 'fetch_timeout']
            assert received == ['/first', '/second']


class Clock:
    def __init__(self):
        self.now = 0
        self.waiters = []
        self.ready = asyncio.Event()

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        self.ready.set()
        await future

    def advance(self, seconds):
        self.now += seconds
        for deadline, future in self.waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)


async def test_discovery_does_not_extend_hour_idle_and_same_session_recovers(tmp_path):
    clock = Clock()
    before = browser_pids()
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Recovered</h1><p>' + 'Public technical documentation. ' * 20 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            browser_clock=clock, browser_sleep=clock.sleep) as (url, _), client(url) as session:
        await asyncio.wait_for(clock.ready.wait(), 10)
        warm = browser_pids() - before
        assert len(warm) == 1
        original = body(await session.call_tool('fetch', {'urls': [base + '/cached']}))['sources'][0]
        assert 'Recovered' in original['content']
        clock.advance(3599)
        await session.call_tool('search', {'query': 'docs', 'content_mode': 'discovery'})
        assert browser_pids() - before == warm
        clock.advance(1)
        await eventually(lambda: browser_pids() == before)
        cached = body(await session.call_tool('fetch', {'urls': [base + '/cached'], 'freshness': 'ordinary'}))['sources'][0]
        assert cached['content'] == original['content']
        assert cached['fetched_at'] == original['fetched_at']
        assert cached['content_version'] == original['content_version']
        data = body(await session.call_tool('fetch', {'urls': [base + '/recreated']}))
        assert 'Recovered' in data['sources'][0]['content']
        assert len(browser_pids() - before) == 1
        assert not warm.intersection(browser_pids())
    await eventually(lambda: browser_pids() == before)


async def test_active_work_survives_idle_deadline_and_queued_discovery_bypasses_it(tmp_path):
    clock = Clock()
    entered = asyncio.Event()
    release = asyncio.Event()
    before = browser_pids()
    async def handler(request):
        if request.path == '/slow':
            entered.set()
            await release.wait()
        return web.Response(content_type='text/html', text='<h1>Protected</h1><p>' + 'Public technical documentation. ' * 20 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options,
            browser_clock=clock, browser_sleep=clock.sleep) as (url, _), client(url) as first, client(url) as second:
        await asyncio.wait_for(clock.ready.wait(), 10)
        warm = browser_pids() - before
        active = asyncio.create_task(first.call_tool('fetch', {'urls': [base + '/slow']}))
        await asyncio.wait_for(entered.wait(), 3)
        clock.advance(3600)
        async with asyncio.timeout(2):
            await second.list_tools()
            data = body(await second.call_tool('search', {'query': 'docs', 'content_mode': 'discovery'}))
        assert len(data['results']) == 10
        assert browser_pids() - before == warm
        release.set()
        assert 'Protected' in body(await active)['sources'][0]['content']
        clock.advance(3599)
        await second.list_tools()
        assert browser_pids() - before == warm
        clock.advance(1)
        await eventually(lambda: browser_pids() == before)


async def test_shutdown_releases_active_browser_and_subrequest_download(tmp_path):
    stop = asyncio.Event()
    entered = asyncio.Event()
    disconnected = asyncio.Event()
    before = browser_pids()
    async def handler(request):
        if request.path == '/hang':
            response = web.StreamResponse(headers={'Content-Type': 'text/plain'})
            await response.prepare(request)
            entered.set()
            try:
                while True:
                    await response.write(b'x')
                    await asyncio.sleep(0.02)
            except ConnectionError:
                disconnected.set()
            return response
        return web.Response(content_type='text/html', text="<h1>Active shutdown</h1><script>fetch('/hang')</script>")
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options, stop=stop) as (url, _):
        try:
            async with client(url) as session:
                task = asyncio.create_task(session.call_tool('fetch', {'urls': [base]}))
                await asyncio.wait_for(entered.wait(), 10)
                assert len(browser_pids() - before) == 1
                stop.set()
                await asyncio.wait_for(disconnected.wait(), 5)
                await eventually(lambda: browser_pids() == before)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        except* httpx.HTTPStatusError as errors:
            assert all(e.response.status_code == 500 for e in errors.exceptions)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert browser_pids() == before


async def test_crashed_browser_reports_failure_then_recovers_without_new_session(tmp_path):
    before = browser_pids()
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Recovery</h1><p>' + 'Public technical documentation. ' * 20 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        initial = body(await session.call_tool('fetch', {'urls': [base + '/initial']}))
        assert 'Recovery' in initial['sources'][0]['content']
        pid, = browser_pids() - before
        psutil.Process(pid).kill()
        await eventually(lambda: pid not in browser_pids())
        failed = body(await session.call_tool('fetch', {'urls': [base + '/failure']}))
        assert failed['sources'][0]['error']['code'] == 'browser_unavailable'
        recovered = body(await session.call_tool('fetch', {'urls': [base + '/recovered']}))
        assert 'Recovery' in recovered['sources'][0]['content']
        assert len(browser_pids() - before) == 1
    await eventually(lambda: browser_pids() == before)
