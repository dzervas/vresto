"""Request-wide budgets survive nested workflows without rejecting queued URLs."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from vresto.acquisition import Acquisition
from vresto.acquisition_policy import AcquisitionOptions
from vresto.contract import FetchInput, SearchInput
from vresto.deadline import remaining, request_deadline
from vresto.server import Settings
from test_acquisition_concurrency import document, until
from test_acquisition_diagnostics import FakeBrowser
from test_retrieval import retrieval
from test_http import cli_response


def test_default_deadlines():
    assert AcquisitionOptions.deadline == Settings.timeout == 300
    assert Settings.research_timeout == 900


@pytest.mark.parametrize('normal,research', [(None, None), ('120', '600')])
def test_main_deadline_environment(monkeypatch, tmp_path, normal, research):
    import os
    import sys
    from vresto import server
    from test_runtime import startup_environment

    monkeypatch.setattr(os, 'environ', startup_environment(tmp_path))
    if normal is not None:
        monkeypatch.setenv('VRESTO_DEADLINE_SECONDS', normal)
    if research is not None:
        monkeypatch.setenv('VRESTO_RESEARCH_TIMEOUT_SECONDS', research)
    # Obsolete queue settings must no longer impose admission limits.
    monkeypatch.setenv('VRESTO_QUEUE_CAPACITY', '1')
    monkeypatch.setenv('VRESTO_QUEUE_WAIT_SECONDS', '.01')
    monkeypatch.setattr(sys, 'argv', ['vresto'])
    captured = []
    monkeypatch.setattr(server, 'create_app', lambda settings: captured.append(settings))
    monkeypatch.setattr(server.uvicorn, 'run', lambda *args, **kwargs: None)
    server.main()
    settings = captured[0]
    assert settings.timeout == settings.acquisition_options.deadline == (float(normal) if normal else 300)
    assert settings.research_timeout == (float(research) if research else 900)


async def test_native_continuation_wait_has_no_independent_navigation_timeout():
    from types import SimpleNamespace
    from vresto.browser import Browser

    response = SimpleNamespace(status=200, headers={'content-type': 'text/html'})
    page = SimpleNamespace(wait_for_load_state=AsyncMock(), evaluate=AsyncMock(return_value=False))
    assert await Browser()._settled_response(page, lambda: response, {}) is response
    page.wait_for_load_state.assert_awaited_once_with('domcontentloaded', timeout=0)


async def test_nested_deadline_inherits_and_resets():
    with request_deadline(900) as outer:
        with request_deadline(.01) as inner:
            assert inner == outer
            assert remaining(300) > 899
        with request_deadline(.01, inherit=False) as cleanup:
            assert cleanup < outer
        assert remaining(300) > 899
    assert remaining(123) == 123


async def test_concurrent_request_deadlines_are_isolated():
    async def request(seconds):
        with request_deadline(seconds) as deadline:
            await asyncio.sleep(0)
            with request_deadline(300) as inherited:
                assert inherited == deadline
                return remaining(300)

    short, long = await asyncio.gather(request(.1), request(900))
    assert 0 < short <= .1
    assert long > 899
    assert remaining(123) == 123


@pytest.mark.parametrize('same_origin', [False, True])
async def test_more_than_old_queue_capacity_eventually_succeeds(same_origin):
    browser = FakeBrowser()
    release = asyncio.Event()

    async def acquire(url, options):
        await release.wait()
        return document(url)

    browser.acquire.side_effect = acquire
    manager = Acquisition(AcquisitionOptions(browser_concurrency=1), browser_factory=lambda: browser)
    urls = [f'https://a.example/{i}' if same_origin else f'https://a{i}.example/' for i in range(20)]
    task = asyncio.create_task(manager.fetch_many(urls))
    try:
        await until(lambda: manager.waiting == 19)
        assert manager.active == 1
        assert not task.done()
        release.set()
        result = await task
        assert [s['content'] for s in result['sources']] == urls
        assert manager.active == manager.waiting == 0
    finally:
        release.set()
        await manager.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_discovery_and_acquisition_share_deadline(tmp_path, monkeypatch):
    browser = FakeBrowser()
    seen = []

    async def discover(_):
        seen.append(remaining(300))
        await asyncio.sleep(.1)
        return {'results': [{'url': 'https://example.org/', 'title': 'Guide'}]}

    async def acquire(url, options):
        seen.append(remaining(300))
        await asyncio.Event().wait()

    browser.acquire.side_effect = acquire
    async with retrieval(tmp_path, source=cli_response('{}'),
                         acquisition_options=AcquisitionOptions(deadline=.3)) as service:
        service.acquisition.browser_factory = lambda: browser
        monkeypatch.setattr(service.discovery, 'search', AsyncMock(side_effect=discover))
        data = await service.search(SearchInput(query='guide', content_mode='highlights', quick_answer=False))
        assert data['results'][0]['error']['code'] == 'fetch_timeout'
        assert 0 < seen[1] < seen[0] - .08
        assert service.acquisition.active == service.acquisition.waiting == 0


async def test_expired_request_does_not_expire_shared_startup():
    browser = FakeBrowser()
    release = asyncio.Event()
    browser.start.side_effect = release.wait
    manager = Acquisition(browser_factory=lambda: browser)
    try:
        with request_deadline(.05):
            source = (await manager.fetch_many(['https://example.org/']))['sources'][0]
        assert source['error']['code'] == 'fetch_timeout'
        assert not manager.startup.done()
        release.set()
        assert await manager.startup
        source = (await manager.fetch_many(['https://example.org/']))['sources'][0]
        assert source['content'] == 'Public text'
    finally:
        release.set()
        await manager.close()


async def test_version_check_and_command_do_not_reset_deadline(tmp_path):
    source = '''import json, sys, time
from pathlib import Path
if "--version" in sys.argv:
    time.sleep(.15)
    print("kagi 0.22.0")
else:
    Path(__file__).with_suffix('.started').touch()
    time.sleep(.15)
    print(json.dumps({"data": []}))
'''
    async with retrieval(tmp_path, source=source, timeout=.25) as service:
        data = await service.search(SearchInput(query='guide', content_mode='discovery', quick_answer=False))
        assert data['error']['code'] == 'discovery_timeout'
        assert (tmp_path / 'cli.started').exists()
        assert not service.discovery.processes.active


async def test_native_replacement_uses_request_not_default_navigation_timeout():
    from aiohttp import web
    from test_fetch import site

    async def handler(request):
        if request.path == '/replacement':
            response = web.StreamResponse(headers={'Content-Type': 'text/html'})
            await response.prepare(request)
            await response.write(b'<main><h1>Delayed public continuation</h1>')
            await asyncio.sleep(.3)
            await response.write(b'<p>This public technical reference documents supported constraints and compatibility requirements.</p></main>')
            await response.write_eof()
            return response
        return web.Response(status=403, content_type='text/html',
            headers={'cf-mitigated': 'challenge'}, text='''<title>Just a moment...</title>
            <script>setTimeout(() => location.href='/replacement', 100)</script>''')

    async with site(handler) as (base, options):
        manager = Acquisition(replace(options, deadline=5))
        try:
            assert await manager.warmup()
            manager.browser.runtime.context.set_default_navigation_timeout(20)
            source = (await manager.fetch_many([base]))['sources'][0]
            assert 'error' not in source, source
            assert 'Delayed public continuation' in source['content']
        finally:
            await manager.close()


async def test_nested_fetch_uses_research_budget_not_normal_timeout(tmp_path):
    browser = FakeBrowser()
    observed = []

    async def acquire(url, options):
        observed.append(remaining(300))
        return document(url)

    browser.acquire.side_effect = acquire
    async with retrieval(tmp_path, source=cli_response('{}'),
                         acquisition_options=replace(AcquisitionOptions(), deadline=.01)) as service:
        service.acquisition.browser_factory = lambda: browser
        with request_deadline(900):
            data = await service.fetch(FetchInput(urls=['https://example.org/']))
        assert data['sources'][0]['content'] == 'https://example.org/'
        assert observed[0] > 899
