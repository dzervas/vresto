import asyncio
from contextlib import asynccontextmanager
import json
import os
import socket
import sys

import pytest
import httpx
import uvicorn
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from kagi_crawler.server import Settings, create_app


@asynccontextmanager
async def running(tmp_path, *, timeout=2, max_bytes=65536, source=None, stop=None, acquisition_options=None,
                  response_budget_bytes=65536, browser_clock=None, browser_sleep=None, **cache_settings):
    # Executable fixture is the external CLI boundary, not a mock of our modules.
    fixture = tmp_path / "cli.py"
    fixture.write_text(source or '''import json, sys
if "--version" in sys.argv:
    print("kagi 0.22.0")
else:
    print(json.dumps({"data": [{"t": 0, "rank": i, "url": f"https://example.org/{i}", "title": f"Source {i}", "snippet": "Provider summary"} for i in range(1, 13)]}))
''')
    app = create_app(Settings(access_token="client-secret", session_token="subscriber-secret",
                              command=(sys.executable, str(fixture)), timeout=timeout,
                              max_output_bytes=max_bytes, acquisition_options=acquisition_options,
                              response_budget_bytes=response_budget_bytes,
                              browser_clock=browser_clock, browser_sleep=browser_sleep,
                              cache_directory=tmp_path / 'cache', **cache_settings))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on",
                                          timeout_graceful_shutdown=0.1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async def shutdown():
        await stop.wait()
        server.should_exit = True
    watcher = asyncio.create_task(shutdown()) if stop is not None else None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield url, fixture
    finally:
        server.should_exit = True
        await task
        sock.close()
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


@asynccontextmanager
async def client(url, token="client-secret", calls=None, timeout=5):
    async def observe(request):
        if calls is not None and request.method == "POST":
            data = json.loads(request.content)
            if data.get("method") == "tools/call":
                calls.append(data["id"])
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                 event_hooks={"request": [observe]}, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                yield session


def body(result):
    return json.loads(result.content[0].text)


async def test_discovery_preserves_provider_order_and_labels_snippets(tmp_path):
    async with running(tmp_path) as (url, _), client(url) as session:
        tools = (await session.list_tools()).tools
        assert [t.name for t in tools] == ["search", "fetch"]
        assert tools[0].inputSchema["properties"]["content_mode"]["default"] == "highlights"
        assert tools[0].inputSchema["additionalProperties"] is False
        assert 'Native Chromium networking' in tools[1].description
        assert 'not SSRF protection' in tools[1].description
        assert 'Stateless GET/HEAD only' not in tools[1].description
        result = await session.call_tool("search", {"query": "Python docs", "content_mode": "discovery"})
        assert not result.isError
        data = body(result)
        assert len(data["results"]) == 10
        assert data["results"][0] == {"provider_rank": 1, "url": "https://example.org/1",
                                       "title": "Source 1", "provider_snippet": "Provider summary"}
        assert data["results"][-1]["provider_rank"] == 10
        assert data["content_mode"] == "discovery"
        assert "result_limit" in data["postfilters"]


async def test_subscriber_controls_are_forwarded_and_update_dates_disclosed(tmp_path):
    source = '''import json, sys
if "--version" in sys.argv:
    print("kagi 0.22.0")
else:
    expected = ["--error-format", "json", "search", "--format", "json", "--limit", "3", "--region", "gb", "--lens", "2", "--from-date", "2026-01-01", "--to-date", "2026-02-01", "--order", "recency", "--verbatim", "--no-personalized", "--", "--query ; $(oops)"]
    assert sys.argv[1:] == expected
    print(json.dumps({"data": [{"t": 0, "rank": 4, "url": "https://example.org/docs", "title": "Docs", "snippet": "Text"}]}))
'''
    async with running(tmp_path, source=source) as (url, _), client(url) as session:
        result = await session.call_tool("search", {"query": "--query ; $(oops)", "num_results": 3,
            "region": "gb", "lens": 2, "from_date": "2026-01-01", "to_date": "2026-02-01",
            "order": "recency", "verbatim": True, "personalized": False, "content_mode": "discovery"})
        assert not result.isError
        assert body(result)["results"][0]["provider_rank"] == 4
        assert body(result)["date_semantics"] == "provider_update_date_not_verified_publication"


@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"query": "x", "num_results": 0}, {"query": "x", "num_results": 21},
    {"query": "x", "num_results": True}, {"query": "x", "num_results": "2"},
    {"query": "x", "content_mode": "unknown"}, {"query": "x", "category": "news"},
    {"query": "x", "time": "decade"}, {"query": "x", "lens": -1},
    {"query": "x", "from_date": "2026-02-30"},
    {"query": "x", "from_date": "2026-02-01", "to_date": "2026-01-01"},
    {"query": "x", "time": "day", "from_date": "2026-01-01"},
])
async def test_invalid_and_unsupported_controls_are_rejected(tmp_path, arguments):
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool("search", arguments)
        assert result.isError
        assert body(result)["error"]["code"] == "invalid_input"


def cli_response(payload, *, failed=False):
    return "import sys\nif '--version' in sys.argv:\n    print('kagi 0.22.0')\nelse:\n    print(" + repr(payload) + ", file=sys.stderr if " + repr(failed) + " else sys.stdout)\n    sys.exit(" + ("1" if failed else "0") + ")\n"


@pytest.mark.parametrize("payload,failed,code", [
    ('not JSON subscriber-secret', False, "discovery_parse_error"),
    ('[' * 2000 + ']' * 2000, False, "discovery_parse_error"),
    ('{"wrong": []}', False, "discovery_parse_error"),
    ('{"data": [{"url": "file:///etc/passwd", "title": "Bad"}]}', False, "discovery_parse_error"),
    ('{"data": [{"url": "https://example.org", "title": 4}]}', False, "discovery_parse_error"),
    ('{"data": [{"url": "https://example.org", "title": "subscriber-secret"}]}', False, "discovery_parse_error"),
    ('{"code": "authentication_error", "message": "expired subscriber-secret", "http_status": 403}', True, "upstream_authentication"),
    ('{"code": "network_error", "http_status": 429, "message": "client-secret"}', True, "rate_limited"),
    ('{"code": "parse_error", "message": "subscriber-secret"}', True, "discovery_parse_error"),
    ('{"code": "network_error", "http_status": 503}', True, "discovery_failed"),
])
async def test_discovery_failures_are_clear_and_never_echo_credentials(tmp_path, caplog, payload, failed, code):
    async with running(tmp_path, source=cli_response(payload, failed=failed)) as (url, _), client(url) as session:
        result = await session.call_tool("search", {"query": "x"})
        assert result.isError
        assert body(result)["error"]["code"] == code
        assert "subscriber-secret" not in result.model_dump_json() + caplog.text
        assert "client-secret" not in result.model_dump_json() + caplog.text


async def test_missing_ranks_and_empty_results_are_not_invented(tmp_path):
    payload = '{"data": [{"t": 0, "rank": null, "url": "https://example.org", "title": "Docs", "snippet": "", "published": "yesterday"}]}'
    async with running(tmp_path, source=cli_response(payload)) as (url, _), client(url) as session:
        data = body(await session.call_tool("search", {"query": "x", "content_mode": "discovery"}))
        assert data["results"][0]["provider_rank"] is None
        assert "published" not in data["results"][0]
        assert "provider_rank_unavailable" in data["warnings"]
    async with running(tmp_path, source=cli_response('{"data": []}')) as (url, _), client(url) as session:
        data = body(await session.call_tool("search", {"query": "x"}))
        assert data["results"] == []
        assert "empty_results_may_indicate_upstream_markup_change" in data["warnings"]


def hanging_cli(tmp_path):
    return f'''import os, sys, time, signal
if "--version" in sys.argv:
    print("kagi 0.22.0")
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open({str(tmp_path / 'pid')!r}, 'w') as f:
        f.write(str(os.getpid()))
    time.sleep(60)
'''


async def wait_for_pid(tmp_path):
    async with asyncio.timeout(3):
        while not (tmp_path / "pid").exists():
            await asyncio.sleep(0.01)
    return int((tmp_path / "pid").read_text())


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_discovery_timeout_reaps_uncooperative_subprocess(tmp_path):
    async with running(tmp_path, source=hanging_cli(tmp_path), timeout=0.15) as (url, _), client(url) as session:
        async with asyncio.timeout(3):
            result = await session.call_tool("search", {"query": "slow"})
        assert result.isError
        assert body(result)["error"]["code"] == "discovery_timeout"
        assert not alive(await wait_for_pid(tmp_path))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_cli_output_is_bounded_on_both_streams(tmp_path, stream):
    source = f'''import sys
if "--version" in sys.argv:
    print("kagi 0.22.0")
else:
    print("x" * 10000000, file=sys.{stream})
'''
    async with running(tmp_path, source=source, max_bytes=1024) as (url, _), client(url) as session:
        async with asyncio.timeout(3):
            result = await session.call_tool("search", {"query": "large"})
        assert result.isError
        assert body(result)["error"]["code"] == "discovery_output_limit"


async def test_mcp_cancellation_reaps_subprocess_and_session_remains_usable(tmp_path):
    calls = []
    async with running(tmp_path, source=hanging_cli(tmp_path)) as (url, _), client(url, calls=calls) as session:
        task = asyncio.create_task(session.call_tool("search", {"query": "slow"}))
        pid = await wait_for_pid(tmp_path)
        await session.send_notification(types.ClientNotification(types.CancelledNotification(
            method="notifications/cancelled", params=types.CancelledNotificationParams(requestId=calls[-1]))))
        async with asyncio.timeout(3):
            try:
                await task
            except Exception as exc:
                assert "cancel" in str(exc).lower()
            while alive(pid):
                await asyncio.sleep(0.01)
        assert [t.name for t in (await session.list_tools()).tools] == ["search", "fetch"]


async def test_unpinned_cli_is_rejected(tmp_path):
    source = '''import sys
if "--version" in sys.argv:
    print("kagi 0.21.0")
else:
    print('{"data": []}')
'''
    async with running(tmp_path, source=source) as (url, _), client(url) as session:
        result = await session.call_tool("search", {"query": "x"})
        assert result.isError
        assert body(result)["error"]["code"] == "discovery_cli_version"


async def test_service_access_is_required_for_mcp_initialization(tmp_path):
    async with running(tmp_path) as (url, _):
        with pytest.raises(BaseExceptionGroup) as error:
            async with client(url, token="wrong"):
                pytest.fail("unauthorized MCP client connected")
        assert "401" in str(error.value.exceptions[0]) or "401" in repr(error.value)
        async with httpx.AsyncClient() as http:
            response = await http.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize"})
            assert response.status_code == 401
            assert response.json() == {"error": "access_rejected"}


async def test_cli_receives_only_upstream_session_without_host_auth_or_preferences(tmp_path):
    source = '''import json, os, sys
if "--version" in sys.argv:
    print("kagi 0.22.0")
else:
    assert os.environ["KAGI_SESSION_TOKEN"] == "subscriber-secret"
    assert not any(k in os.environ for k in ("KAGI_API_KEY", "KAGI_API_TOKEN", "KAGI_CRAWLER_ACCESS_TOKEN", "KAGI_BASE_URL"))
    assert all("client-secret" not in value for value in os.environ.values())
    assert "subscriber-secret" not in str(sys.argv)
    assert not os.path.exists(os.environ["KAGI_CONFIG"])
    assert os.environ["RUST_LOG"] == "off"
    print(json.dumps({"data": [{"t": 0, "rank": 1, "url": "https://example.org", "title": "Isolated", "snippet": ""}]}))
'''
    async with running(tmp_path, source=source) as (url, _), client(url) as session:
        result = await session.call_tool("search", {"query": "x", "content_mode": "discovery"})
        assert not result.isError
        assert body(result)["results"][0]["title"] == "Isolated"


async def test_server_shutdown_reaps_active_discovery(tmp_path):
    stop = asyncio.Event()
    async with running(tmp_path, source=hanging_cli(tmp_path), timeout=30, stop=stop) as (url, _):
        try:
            async with client(url) as session:
                task = asyncio.create_task(session.call_tool("search", {"query": "slow"}))
                pid = await wait_for_pid(tmp_path)
                stop.set()
                async with asyncio.timeout(3):
                    while alive(pid):
                        await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        except* httpx.HTTPStatusError as errors:
            # Uvicorn ends in-flight HTTP responses when the grace period expires.
            assert all(e.response.status_code == 500 for e in errors.exceptions)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not alive(pid)


@pytest.mark.parametrize("window,order", [("day", "default"), ("week", "website"),
                                          ("month", "trackers"), ("year", "recency")])
async def test_relative_time_order_and_personalization_controls(tmp_path, window, order):
    source = f'''import sys
if "--version" in sys.argv:
    print("kagi 0.22.0")
else:
    assert sys.argv[1:] == ["--error-format", "json", "search", "--format", "json", "--limit", "10", "--time", {window!r}, "--order", {order!r}, "--personalized", "--", "x"]
    print('{{"data": []}}')
'''
    async with running(tmp_path, source=source) as (url, _), client(url) as session:
        result = await session.call_tool("search", {"query": "x", "time": window,
                                                    "order": order, "personalized": True})
        assert not result.isError
        assert body(result)["results"] == []


def held_cli(tmp_path):
    return f'''import json, os, sys, time
if "--version" in sys.argv:
    print("kagi 0.22.0")
else:
    from pathlib import Path
    Path({str(tmp_path)!r}, 'pid-' + sys.argv[-1]).write_text(str(os.getpid()))
    while not Path({str(tmp_path / 'release')!r}).exists():
        time.sleep(.01)
    print(json.dumps({{"data": []}}))
'''


async def wait_for_searches(tmp_path, count):
    async with asyncio.timeout(3):
        while len(list(tmp_path.glob('pid-*'))) < count:
            await asyncio.sleep(.01)


async def test_discovery_admission_bounds_clients_and_rejects_excess_without_spawn(tmp_path):
    source = held_cli(tmp_path)
    async with running(tmp_path, source=source, timeout=3) as (url, _), client(url) as first, client(url) as second:
        tasks = []
        try:
            for i in range(2):
                tasks.append(asyncio.create_task(first.call_tool('search', {'query': str(i), 'content_mode': 'discovery'})))
                async with asyncio.timeout(3):
                    while not (tmp_path / f'pid-{i}').exists():
                        await asyncio.sleep(.01)
            for i in range(2, 10):
                tasks.append(asyncio.create_task(second.call_tool('search', {'query': str(i), 'content_mode': 'discovery'})))
            await asyncio.sleep(.2)
            excess = await first.call_tool('search', {'query': 'excess', 'content_mode': 'discovery'})
            assert body(excess)['error']['code'] == 'discovery_queue_full'
            assert len(list(tmp_path.glob('pid-*'))) == 2
            assert 'stderr' not in excess.model_dump_json()
            (tmp_path / 'release').touch()
            results = await asyncio.gather(*tasks)
            assert all(not r.isError for r in results)
            assert len(list(tmp_path.glob('pid-*'))) == 10
        finally:
            (tmp_path / 'release').touch()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('timeout,code', [(0.8, 'discovery_timeout'), (20, 'discovery_queue_timeout')])
async def test_discovery_wait_has_its_own_limit_and_shares_request_deadline(tmp_path, timeout, code):
    source = held_cli(tmp_path)
    calls = []
    async with running(tmp_path, source=source, timeout=timeout) as (url, _), client(url, timeout=25, calls=calls) as session:
        active = [asyncio.create_task(session.call_tool('search', {'query': str(i)})) for i in range(2)]
        await wait_for_searches(tmp_path, 2)
        started = asyncio.get_running_loop().time()
        queued = await session.call_tool('search', {'query': 'queued', 'content_mode': 'discovery'})
        elapsed = asyncio.get_running_loop().time() - started
        assert body(queued)['error']['code'] == code
        assert elapsed < (1.6 if timeout < 1 else 11)
        for identifier in calls[:2]:
            await session.send_notification(types.ClientNotification(types.CancelledNotification(
                method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=identifier))))
        await asyncio.gather(*active, return_exceptions=True)


async def test_cancelled_discovery_waiter_releases_capacity_without_running_cli(tmp_path):
    calls = []
    source = held_cli(tmp_path)
    async with running(tmp_path, source=source, timeout=3) as (url, _), client(url, calls=calls) as session:
        tasks = [asyncio.create_task(session.call_tool('search', {'query': str(i)})) for i in range(2)]
        try:
            await wait_for_searches(tmp_path, 2)
            tasks.extend(asyncio.create_task(session.call_tool('search', {'query': str(i)})) for i in range(2, 10))
            await asyncio.sleep(.2)
            await session.send_notification(types.ClientNotification(types.CancelledNotification(
                method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=calls[-1]))))
            await asyncio.gather(tasks[-1], return_exceptions=True)
            replacement = asyncio.create_task(session.call_tool('search', {'query': 'replacement'}))
            await asyncio.sleep(.1)
            excess = await session.call_tool('search', {'query': 'excess'})
            assert body(excess)['error']['code'] == 'discovery_queue_full'
            assert not replacement.done()
            assert len(list(tmp_path.glob('pid-*'))) == 2
            assert not (tmp_path / 'pid-9').exists()
            tasks.append(replacement)
        finally:
            for identifier in calls:
                await session.send_notification(types.ClientNotification(types.CancelledNotification(
                    method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=identifier))))
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_shutdown_reaps_admitted_discovery_and_never_spawns_waiters(tmp_path):
    stop = asyncio.Event()
    tasks = []
    async with running(tmp_path, source=held_cli(tmp_path), timeout=30, stop=stop) as (url, _):
        try:
            async with client(url) as session:
                tasks = [asyncio.create_task(session.call_tool('search', {'query': str(i)})) for i in range(10)]
                await wait_for_searches(tmp_path, 2)
                await asyncio.sleep(.2)
                pids = [int(p.read_text()) for p in tmp_path.glob('pid-*')]
                stop.set()
                async with asyncio.timeout(3):
                    while any(alive(pid) for pid in pids):
                        await asyncio.sleep(.01)
                await asyncio.gather(*tasks, return_exceptions=True)
        except* httpx.HTTPStatusError as errors:
            # Expected: Uvicorn ends in-flight responses before SDK shutdown.
            assert all(e.response.status_code == 500 for e in errors.exceptions)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert len(list(tmp_path.glob('pid-*'))) == 2
    assert not any(alive(pid) for pid in pids)


@pytest.mark.parametrize("token", ["client-secret", "subscriber-secret", "subscriber%2Dsecret"])
async def test_search_rejects_credentials_before_any_cli_or_upstream_work(tmp_path, caplog, token):
    marker = tmp_path / 'invoked'
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('kagi 0.22.0')\n"
    async with running(tmp_path, source=source) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': 'lookup ' + token, 'content_mode': 'discovery'})
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'
        assert not marker.exists()  # External executable never receives client input.
        assert token not in result.model_dump_json() + caplog.text
