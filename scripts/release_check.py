"""Deterministic release gate and repeatable native/container resource measurement.

Run with installed dependencies: python scripts/release_check.py --cache /tmp/gate-cache
Container driver: python scripts/container_check.py IMAGE
No upstream sessions, external sites or paid calls. Exact loopback exception exists
only in this fixture bootstrap, never in the production entry point. Observations
use real TCP MCP clients and external process trees, not browser ownership fields.
RSS sums shared pages multiple times; sample interval 20ms may miss shorter peaks.
Numbers are sandbox observations, NOT a RAM limit or deployment sizing guarantee.
Historical facts below predate user-approved removal of interception; they are
NOT measurements of the current native-networking implementation. Repeat this
gate for current release observations; native resource traffic has no Python
stream/header/decompression bounds. Native-networking rerun (2026-10-13, same
host, overlapping suite): cold/cache/refresh 1390.06/43.83/643.83 ms, 1,673,199,616
bytes sampled tree peak (shared pages double-counted), 108 observed descendants,
none surviving live; fresh-process cache/evidence persistence passed. These tiny
fixtures are not deployment memory sizing or a native resource traffic cap.
Raw facts, 2026-10-13 sandbox x86_64 / 32 logical CPUs / Python 3.12.13,
locked dependencies, system node + Chromium 153.0.8010.12/r1243. Three fresh
cache runs, before the subsequent pytest run; host load is not controlled.
No container/deployment measurement or real upstream discovery is implied.
RSS columns are bytes; latency columns ms:
run,cold_initialize_search,warm_cached_fetch,warm_browser_refresh,queue_timeout,rss_before,rss_warm_idle,rss_after_idle,rss_after_shutdown,active_peak
1,1624.23,48.83,813.28,161.76,133132288,529784832,146784256,151945216,654401536
2,1541.62,47.02,832.77,161.90,133152768,529498112,147005440,152289280,653877248
3,1574.85,51.78,756.60,172.29,133017600,527810560,146694144,151912448,652115968
Default queue, nine calls across two clients (sorted completion latencies, ms):
1: 1036.14,1766.84,2529.24,3289.04,4010.83,4771.58,5543.74,6330.40,7045.39
2: 929.40,1670.53,2331.90,3095.78,3886.10,4612.53,5362.11,6112.06,6842.65
3: 960.02,1710.79,2392.91,3134.67,3898.04,4671.30,5419.36,6196.79,6972.42
Each observed one browser, successful serial completion, capacity=1 excess
queue_full and queued queue_timeout, idle/recreated/crashed/recovered browsers,
and no surviving child processes. A fresh Python process using run 3's cache
resolved text and exact evidence with zero fixture requests. The later observed
PID+birth-time gate strengthens cleanup checks against reparented descendants.
That later gate also passed: 41 sampled descendants, zero surviving live ones;
fresh-process persistence passed with zero requests and one sampled driver.
Its raw cold/cache/refresh ms: 1236.64/44.11/700.20; RSS before/warm/idle/shutdown/
peak bytes: 133120000/529387520/146722816/154099712/653606912. Default nine-call
completion ms: 810.96,1480.56,2113.36,2801.75,3477.61,4138.76,4794.58,5465.34,6099.89.
This later run overlapped native test activity; do not infer idle-host performance.
Imports precede rss_before; peak covers PDF and traversal plus browser work.
Repeat on deployment hardware: tiny fixtures are NOT heavy-site/PDF upper bounds.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import platform
from pathlib import Path
import socket
import sys
import tempfile
import time

from aiohttp import web
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import psutil
import uvicorn

from kagi_crawler.server import Settings, create_app
from kagi_crawler.acquisition_policy import AcquisitionOptions


class Clock:
    def __init__(self):
        self.now = 0
        self.waiters = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        await future

    def advance(self):
        self.now += 3601
        for deadline, future in self.waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)


def pdf_fixture():
    # Literal text-bearing PDF with real offsets, not OCR or a model generator.
    stream = b'BT /F1 12 Tf 72 720 Td (Version 2 requires TLS. Never disable verification.) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents 4 0 R >>',
        f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream']
    content, offsets = b'%PDF-1.4\n', [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content += f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(content)
    content += b'xref\n0 5\n0000000000 65535 f \n'
    content += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    return content + f'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


def processes():
    return psutil.Process().children(recursive=True)


def browsers():
    found = set()
    for process in processes():
        try:
            if 'chrome' in process.name() and process.parent().name() == 'node':
                found.add(process.pid)
        except psutil.NoSuchProcess:
            pass
    return found


def rss():
    total = 0
    for process in [psutil.Process(), *processes()]:
        try:
            total += process.memory_info().rss
        except psutil.NoSuchProcess:
            pass
    return total


async def eventually(predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(.02)


@asynccontextmanager
async def client(url):
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer fixture-access'},
                                 trust_env=False, timeout=40) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def server(cache, cli, clock, *, default_queue=False):
    queue = {} if default_queue else {'queue_capacity': 1, 'queue_wait': .15}
    options = AcquisitionOptions(fixture_destinations=(('127.0.0.1', 18081, '127.0.0.1'),), **queue)
    app = create_app(Settings(access_token='fixture-access', session_token='fixture-session',
        command=(sys.executable, str(cli)), cache_directory=cache,
        acquisition_options=options, browser_clock=clock, browser_sleep=clock.sleep))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}/mcp'
    http = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False,
                                       timeout_graceful_shutdown=.1))
    task = asyncio.create_task(http.serve(sockets=[sock]))
    try:
        await eventually(lambda: http.started or task.done())
        if task.done():
            await task
        yield url
    finally:
        http.should_exit = True
        await task
        sock.close()


def data(result):
    value = json.loads(result.content[0].text)
    assert not result.isError, value.get('error')
    assert 'fixture-session' not in result.model_dump_json()
    assert 'fixture-access' not in result.model_dump_json()
    return value


async def run(cache, verify_persisted=False):
    cache.mkdir(parents=True, exist_ok=True)
    if not verify_persisted and any(cache.glob('*.json')):
        raise SystemExit('Cold measurement requires a new empty cache; use --verify-persisted for reuse.')
    before = {p.pid for p in processes()}
    facts = {'sample_interval_ms': 20, 'rss_before_start_bytes': rss(),
             'architecture': platform.machine(), 'logical_cpus': psutil.cpu_count(),
             'python': platform.python_version()}
    samples = []
    observed = {}
    async def sample():
        while True:
            samples.append(rss())
            for process in processes():
                try:
                    observed[process.pid] = process.create_time()
                except psutil.NoSuchProcess:
                    pass
            await asyncio.sleep(.02)

    def surviving_observed():
        # Also observe descendants later reparented away from this process.
        # PID + creation time avoids mistaking a recycled PID for an orphan.
        for pid, created in observed.items():
            try:
                process = psutil.Process(pid)
                if process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE:
                    return True
            except psutil.NoSuchProcess:
                pass
        return False
    sampler = asyncio.create_task(sample())
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def page(request):
        requests.append(request.path)
        assert 'fixture-session' not in str(request.headers)
        assert 'fixture-access' not in str(request.headers)
        if request.path == '/document.pdf':
            return web.Response(content_type='application/pdf', body=pdf_fixture())
        if request.path == '/hold':
            entered.set()
            await release.wait()
            return web.Response(status=404)
        return web.Response(content_type='text/html', text='''<title>Release fixture</title>
        <h1>Version 2</h1><p>Identifier retry_limit is 0. Warning: do not enable legacy mode.</p>
        <pre><code>retry_limit = 0</code></pre><table><tr><th>Option</th><th>Default</th></tr>
        <tr><td>retry_limit</td><td>0</td></tr></table><p id="js"></p>
        <script>document.querySelector('#js').textContent='Rendered JavaScript evidence';</script>
        <a href='/related'>Related reference</a>''')
    fixture = web.Application()
    fixture.router.add_get('/{path:.*}', page)
    runner = web.AppRunner(fixture)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 18081)
    await site.start()
    try:
        with tempfile.TemporaryDirectory() as work:
            cli = Path(work) / 'cli.py'
            cli.write_text('import json,sys\nif "--version" in sys.argv: print("kagi 0.22.0")\n'
                'else: print(json.dumps({"data":[{"t":0,"rank":1,"title":"Fixture",'
                '"url":"http://127.0.0.1:18081/docs","snippet":"Provider summary"}]}))\n')
            clock = Clock()
            async with server(cache, cli, clock) as url:
                async with httpx.AsyncClient(trust_env=False) as http:
                    assert (await http.post(url, json={})).status_code == 401
                start = time.perf_counter()
                async with client(url) as first, client(url) as second:
                    assert {t.name for t in (await first.list_tools()).tools} == {'search', 'fetch'}
                    if verify_persisted:
                        source = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/docs'],
                                                                      'freshness': 'cache_only'}))['sources'][0]
                        assert source['cache_state'] == 'hit', source
                        assert 'retry_limit' in source['content']
                        evidence = (cache / 'gate-evidence-id').read_text()
                        expanded = data(await first.call_tool('fetch', {'evidence_ids': [evidence]}))
                        assert 'legacy mode' in str(expanded['sources'][0]['highlights'])
                        assert not requests, requests
                        facts['persisted_cache_and_evidence'] = True
                    else:
                        result = data(await first.call_tool('search', {'query': 'retry_limit'}))
                        source = result['results'][0]
                        assert 'error' not in source, source
                        assert source['highlights'], source
                        assert 'legacy mode' in str(source['highlights'])
                        evidence = source['highlights'][0]['evidence_id']
                        (cache / 'gate-evidence-id').write_text(evidence)
                        facts['cold_initialize_search_ms'] = round((time.perf_counter() - start) * 1000, 2)
                        await eventually(lambda: len(browsers()) == 1)
                        warm_browser = browsers()
                        facts['rss_warm_idle_bytes'] = rss()
                        start = time.perf_counter()
                        warm = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/docs']}))
                        facts['warm_cached_fetch_ms'] = round((time.perf_counter() - start) * 1000, 2)
                        assert warm['sources'][0]['cache_state'] == 'hit'
                        assert 'Rendered JavaScript evidence' in warm['sources'][0]['content']
                        start = time.perf_counter()
                        refreshed = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/docs'],
                                                                        'freshness': 'fresh_only'}))
                        assert 'error' not in refreshed['sources'][0], refreshed
                        facts['warm_browser_refresh_ms'] = round((time.perf_counter() - start) * 1000, 2)
                        pdf = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/document.pdf'],
                                                                  'query': 'TLS'}))['sources'][0]
                        assert pdf['document_type'] == 'pdf', pdf
                        assert pdf['highlights'][0]['pages'] == [1]
                        assert 'Never disable verification.' in pdf['highlights'][0]['text']
                        traversed = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/docs'],
                            'traversal': {'origin': 'http://127.0.0.1:18081', 'path_prefix': '/', 'max_pages': 2}}))
                        assert len(traversed['sources']) == 2, traversed
                        forbidden = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18082/']}))
                        assert forbidden['sources'][0]['error']['code'] == 'policy_rejected'
                        # Deterministic multi-client pressure: one active, one queued,
                        # an excess call rejected, then bounded queue timeout.
                        start = time.perf_counter()
                        active = asyncio.create_task(first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/hold']}))
                        await asyncio.wait_for(entered.wait(), 5)
                        queued = asyncio.create_task(second.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/hold']}))
                        await asyncio.sleep(.04)
                        excess = data(await second.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/hold']}))
                        assert excess['sources'][0]['error']['code'] == 'queue_full'
                        assert data(await queued)['sources'][0]['error']['code'] == 'queue_timeout'
                        facts['queue_timeout_observed_ms'] = round((time.perf_counter() - start) * 1000, 2)
                        facts['queue'] = {'active': 1, 'capacity': 1, 'excess': 'queue_full', 'queued': 'queue_timeout'}
                        release.set()
                        assert data(await active)['sources'][0]['error']['code'] == 'fetch_failed'
                        clock.advance()
                        await eventually(lambda: not browsers())
                        await eventually(lambda: not ({p.pid for p in processes()} - before))
                        facts['rss_after_idle_teardown_bytes'] = rss()
                        # Same MCP session recreates browser; retained evidence does
                        # not depend on warmup or current page acquisition.
                        expanded = data(await first.call_tool('fetch', {'evidence_ids': [evidence]}))
                        assert 'legacy mode' in str(expanded['sources'][0]['highlights'])
                        await eventually(lambda: len(browsers()) == 1)
                        assert browsers().isdisjoint(warm_browser)
                        recreated = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/docs'],
                                                                        'freshness': 'fresh_only'}))
                        assert 'error' not in recreated['sources'][0], recreated
                        # External browser crash; first acquisition may report loss,
                        # subsequent request recreates without MCP reconnection.
                        for pid in browsers():
                            psutil.Process(pid).kill()
                        await eventually(lambda: not browsers())
                        failed = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/docs'], 'freshness': 'fresh_only'}))
                        assert failed['sources'][0]['error']['code'] == 'browser_unavailable'
                        recovered = data(await first.call_tool('fetch', {'urls': ['http://127.0.0.1:18081/docs'], 'freshness': 'fresh_only'}))
                        assert 'error' not in recovered['sources'][0], recovered
            await eventually(lambda: not ({p.pid for p in processes()} - before))
            if not verify_persisted:
                # Actual default queue: two independent clients, nine concurrent
                # uncached acquisitions (one active + eight waiting), one browser.
                async with server(cache, cli, Clock(), default_queue=True) as url:
                    async with client(url) as first, client(url) as second:
                        await eventually(lambda: len(browsers()) == 1)
                        latencies = []
                        async def load(index):
                            started = time.perf_counter()
                            session = first if index % 2 else second
                            result = data(await session.call_tool('fetch', {
                                'urls': [f'http://127.0.0.1:18081/load?id={index}'], 'freshness': 'fresh_only'}))
                            assert 'error' not in result['sources'][0], result
                            latencies.append(round((time.perf_counter() - started) * 1000, 2))
                            assert len(browsers()) == 1
                        await asyncio.gather(*(load(i) for i in range(9)))
                        facts['default_queue_nine_client_calls_ms'] = sorted(latencies)
                await eventually(lambda: not ({p.pid for p in processes()} - before))
            await eventually(lambda: not surviving_observed())
            facts['rss_after_server_shutdown_bytes'] = rss()
            facts['cleanup'] = 'no surviving observed live descendants (including reparented)'
            facts['observed_descendant_count'] = len(observed)
    finally:
        release.set()
        await runner.cleanup()
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    facts['sampled_active_peak_rss_bytes'] = max(samples, default=rss())
    print(json.dumps(facts, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--verify-persisted', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(args.cache, args.verify_persisted))
