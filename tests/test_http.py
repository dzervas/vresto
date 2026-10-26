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

from vresto.server import Settings, create_app
from vresto.acquisition_policy import AcquisitionOptions


@asynccontextmanager
async def running(tmp_path, *, timeout=2, max_bytes=65536, source=None, stop=None, acquisition_options=None,
                  response_budget_bytes=65536, browser_clock=None, browser_sleep=None,
                  access_token='client-secret', **cache_settings):
    # Executable fixture is the external CLI boundary, not a mock of our modules.
    fixture = tmp_path / "cli.py"
    fixture.write_text(source or '''import json, sys
if "--version" in sys.argv:
    print("kagi 0.23.0")
else:
    print(json.dumps({"data": [{"t": 0, "rank": i, "url": f"https://example.org/{i}", "title": f"Source {i}", "snippet": "Provider summary"} for i in range(1, 13)]}))
''')
    app = create_app(Settings(access_token=access_token, session_token="subscriber-secret",
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


# Observe bounded service responses rather than preempting the canonical 300s
# deadline with a 5s transport timeout. Native fixture latency reproduced that
# mismatch on pre/post-optimization code. Timing/cancellation tests retain their
# independent watchdogs and explicit observer overrides.
@asynccontextmanager
async def client(url, token="client-secret", calls=None, timeout=Settings.timeout + AcquisitionOptions.deadline + 5, wire=None):
    async def observe(request):
        if calls is not None and request.method == "POST":
            data = json.loads(request.content)
            if data.get("method") == "tools/call":
                calls.append(data["id"])
    async def observe_response(response):
        if wire is not None and response.headers.get('content-type', '').startswith('application/json'):
            wire.append(await response.aread())
    # Even a no-op response hook changes cancellation timing; observe only on request.
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"} if token else {},
                                 event_hooks={"request": [observe],
                                              "response": [observe_response] if wire is not None else []}, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                yield session


def body(result):
    assert result.content == []
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


async def test_discovery_preserves_provider_order_and_labels_snippets(tmp_path):
    async with running(tmp_path) as (url, _), client(url) as session:
        tools = (await session.list_tools()).tools
        assert [t.name for t in tools] == ["search", "fetch", "expand", "crawl", "research"]
        assert tools[0].inputSchema["properties"]["content_mode"]["default"] == "discovery"
        assert tools[0].inputSchema["additionalProperties"] is False
        assert all(300 <= len(t.description) <= 600 for t in tools)
        expected = [
            {'query', 'num_results', 'content_mode', 'freshness', 'quick_answer'},
            {'urls', 'query', 'content_mode', 'freshness'},
            {'evidence_ids'}, {'url', 'path_prefix', 'max_pages'},
            {'query', 'tier', 'verify_citations'},
        ]
        assert [set(t.inputSchema['properties']) for t in tools] == expected
        result = await session.call_tool("search", {"query": "Python docs", "quick_answer": False})
        assert not result.isError
        data = body(result)
        assert len(data["results"]) == 10
        assert data["results"][0] == {"provider_rank": 1, "url": "https://example.org/1",
                                       "title": "Source 1", "provider_snippet": "Provider summary"}
        assert data["results"][-1]["provider_rank"] == 10
        assert data["content_mode"] == "discovery"
        assert "result_limit" in data["postfilters"]


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
    return "import sys\nif '--version' in sys.argv:\n    print('kagi 0.23.0')\nelse:\n    print(" + repr(payload) + ", file=sys.stderr if " + repr(failed) + " else sys.stdout)\n    sys.exit(" + ("1" if failed else "0") + ")\n"


def hanging_cli(tmp_path):
    return f'''import os, sys, time, signal
if "--version" in sys.argv:
    print("kagi 0.23.0")
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
    print("kagi 0.23.0")
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
        assert [t.name for t in (await session.list_tools()).tools] == ["search", "fetch", "expand", "crawl", "research"]


async def test_service_access_is_required_for_mcp_initialization(tmp_path):
    async with running(tmp_path) as (url, _):
        with pytest.raises(BaseExceptionGroup) as error:
            async with client(url, token="wrong"):
                pytest.fail("unauthorized MCP client connected")
        assert "401" in str(error.value.exceptions[0]) or "401" in repr(error.value)
        async with httpx.AsyncClient() as http:
            response = await http.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize"})
            assert response.status_code == 401
            assert response.json()['error'] == 'invalid_token'
            assert response.headers['www-authenticate'].startswith('Bearer ')


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


def held_cli(tmp_path):
    return f'''import json, os, sys, time
if "--version" in sys.argv:
    print("kagi 0.23.0")
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


async def test_discovery_admission_queues_beyond_old_capacity_without_extra_spawn(tmp_path):
    source = held_cli(tmp_path)
    async with running(tmp_path, source=source, timeout=5) as (url, _), client(url) as first, client(url) as second:
        tasks = []
        try:
            for i in range(2):
                tasks.append(asyncio.create_task(first.call_tool('search', {'query': str(i), 'content_mode': 'discovery', 'quick_answer': False})))
            await wait_for_searches(tmp_path, 2)
            tasks.extend(asyncio.create_task(second.call_tool('search', {
                'query': str(i), 'content_mode': 'discovery', 'quick_answer': False})) for i in range(2, 15))
            await asyncio.sleep(.2)
            assert len(list(tmp_path.glob('pid-*'))) == 2
            assert not any(task.done() for task in tasks)
            (tmp_path / 'release').touch()
            results = await asyncio.gather(*tasks)
            assert all(not r.isError for r in results)
            assert len(list(tmp_path.glob('pid-*'))) == 15
        finally:
            (tmp_path / 'release').touch()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_discovery_wait_shares_request_deadline(tmp_path):
    source = held_cli(tmp_path)
    calls = []
    async with running(tmp_path, source=source, timeout=.8) as (url, _), client(url, calls=calls) as session:
        active = [asyncio.create_task(session.call_tool('search', {'query': str(i), 'quick_answer': False})) for i in range(2)]
        await wait_for_searches(tmp_path, 2)
        started = asyncio.get_running_loop().time()
        queued = await session.call_tool('search', {'query': 'queued', 'content_mode': 'discovery', 'quick_answer': False})
        elapsed = asyncio.get_running_loop().time() - started
        assert body(queued)['error']['code'] == 'discovery_timeout'
        assert elapsed < 1.6
        await asyncio.gather(*active, return_exceptions=True)


async def test_cancelled_discovery_waiter_never_runs_cli(tmp_path):
    calls = []
    source = held_cli(tmp_path)
    async with running(tmp_path, source=source, timeout=5) as (url, _), client(url, calls=calls) as session:
        tasks = [asyncio.create_task(session.call_tool('search', {'query': str(i), 'quick_answer': False})) for i in range(2)]
        try:
            await wait_for_searches(tmp_path, 2)
            tasks.extend(asyncio.create_task(session.call_tool('search', {
                'query': str(i), 'content_mode': 'discovery', 'quick_answer': False})) for i in range(2, 12))
            # An outgoing HTTP hook precedes server admission. Wait until the
            # responder exists before sending MCP cancellation for the last call.
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer client-secret'}) as http:
                async with asyncio.timeout(3):
                    while True:
                        health = (await http.get(url.replace('/mcp', '/health'))).json()
                        if health['discovery']['waiting'] == 10:
                            break
                        await asyncio.sleep(.01)
            await session.send_notification(types.ClientNotification(types.CancelledNotification(
                method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=calls[-1]))))
            await asyncio.gather(tasks[-1], return_exceptions=True)
            replacement = asyncio.create_task(session.call_tool('search', {
                'query': 'replacement', 'content_mode': 'discovery', 'quick_answer': False}))
            tasks.append(replacement)
            await asyncio.sleep(.1)
            assert not replacement.done()
            assert len(list(tmp_path.glob('pid-*'))) == 2
            (tmp_path / 'release').touch()
            results = await asyncio.gather(*tasks[:-2], replacement)
            assert all(not result.isError for result in results)
            assert not (tmp_path / 'pid-11').exists()
            assert (tmp_path / 'pid-replacement').exists()
        finally:
            (tmp_path / 'release').touch()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_shutdown_reaps_admitted_discovery_and_never_spawns_waiters(tmp_path):
    stop = asyncio.Event()
    tasks = []
    async with running(tmp_path, source=held_cli(tmp_path), timeout=30, stop=stop) as (url, _):
        try:
            async with client(url) as session:
                tasks = [asyncio.create_task(session.call_tool('search', {'query': str(i), 'quick_answer': False})) for i in range(10)]
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
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('kagi 0.23.0')\n"
    async with running(tmp_path, source=source) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': 'lookup ' + token, 'content_mode': 'discovery'})
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'
        assert not marker.exists()  # External executable never receives client input.
        assert token not in result.model_dump_json() + caplog.text


@pytest.mark.parametrize('outcome', ['success', 'invalid_input', 'discovery_failed'])
async def test_structured_only_result_is_unchanged_on_http_wire(tmp_path, outcome):
    from vresto.budget import wire_bytes

    snippet = '漢字 "quoted"\nqualification: do not enable legacy mode.'
    source = cli_response(json.dumps({'data': [{'url': 'https://example.org/',
        'title': 'Guide', 'snippet': snippet, 'rank': 1}]}))
    if outcome == 'discovery_failed':
        source = cli_response('upstream unavailable', failed=True)
    arguments = {'query': '' if outcome == 'invalid_input' else 'docs', 'content_mode': 'discovery'}
    wire = []
    async with running(tmp_path, source=source) as (url, _), client(url, wire=wire) as session:
        assert all(tool.outputSchema is None for tool in (await session.list_tools()).tools)
        result = await session.call_tool('search', arguments)
        # Inspect actual HTTP response bytes, not just the SDK-decoded object.
        raw = next(json.loads(packet)['result'] for packet in reversed(wire)
                   if 'result' in json.loads(packet))
        assert raw == result.model_dump(exclude_none=True, by_alias=True)
        assert set(raw) == {'content', 'structuredContent', 'isError'}
        assert raw['content'] == []
        assert raw['isError'] is (outcome != 'success')
        # SDK transport uses the same compact UTF-8 serialization as the gate,
        # including isError=true (one byte shorter than false) for failures.
        encoded = json.dumps(raw, ensure_ascii=False, separators=(',', ':')).encode()
        assert encoded in wire[-1]
        assert len(encoded) == wire_bytes(raw['structuredContent'])
        if outcome == 'success':
            assert raw['structuredContent']['results'][0]['provider_snippet'] == snippet
        else:
            assert raw['structuredContent']['error']['code'] == outcome
