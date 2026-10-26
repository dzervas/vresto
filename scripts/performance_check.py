"""Local real-HTTP MCP profile, reusing the release gate's native fixture seams.

python scripts/performance_check.py --output /tmp/hkq-before.json
Default predeclared matrix: 3 independent Python processes, empty caches and
browser lifetimes; 3 warm repetitions per corpus/workload in each process.
No upstream discovery, Exa, optional models, deployment or sizing claim.
Source module hashes, both harness hashes, uv.lock hash, exact argv/import roots
and all fixture bodies are bound into the JSON provenance. Fixed baseline base
revision plus source-tree hash identifies uncommitted optimized source too.
Local reproduction uses the supplied native library/font/node/browser paths and
the workspace's editable installation and .venv Python; never shared uv auto-sync.
Operator-only deployment reproduction (requires an existing Docker daemon,
candidate image digest built from ticket/hkq, actual deployment CPU/memory caps):
  name=hkq-profile-$(date +%s)
  docker run --name "$name" --init --network none --cap-drop ALL \
    --security-opt no-new-privileges --shm-size 256m \
    --cpus "$DEPLOYMENT_CPUS" --memory "$DEPLOYMENT_MEMORY" \
    --mount "type=bind,src=$PWD/scripts,dst=/gate,readonly" \
    --mount "type=bind,src=$PWD/uv.lock,dst=/uv.lock,readonly" \
    --entrypoint /app/.venv/bin/python "$IMAGE_DIGEST" \
    /gate/performance_check.py --output /tmp/hkq-deployment.json
  docker cp "$name":/tmp/hkq-deployment.json ./hkq-deployment.json
  docker rm "$name"
Run on the deployment node, serialized against other browser suites, once each
for baseline and candidate images at the SAME resource limits. Supply node/CPU
identity, cgroup limits and image digests with the JSON. This fixture gate does
not replace release_check/container_check or measure upstream discovery/Exa.
Stage wrappers are benchmark-only, fixed labels and durations/counts: no inputs,
outputs, exception strings, URLs or page text enter diagnostics. Timings use
perf_counter (monotonic wall time), not the release harness's fake idle clock.
Inclusive spans overlap (startup/admission/render/expansion); never sum them or
infer stage causality by end-to-end subtraction. Cold startup is distinct from
warm Browser.start ensure-ready no-ops. Queue episodes hold one active request
and two contenders until explicit release; all share their request deadlines,
without independent capacity/wait rejection. Historical profiles used the old
capacity=1/wait=150ms policy and are not directly comparable for queue latency.
Per-call fixture visit deltas overlap in queue episodes; paired outcomes, not
those deltas, establish admission. Nine samples have weak tails (p95=max). Extraction covers semantic
normalization and Markdown conversion together, not DOM serialization.
RSS includes this Python client/server/fixture process and recursive descendants,
double-counts shared pages, excludes the coordinator, samples every 20ms, can miss
short peaks, and is not PSS, cgroup memory or a deployment resource bound.
"""
# Keep bootstrap before heavy imports so cold process import cost is observable.
import time
BOOT = time.perf_counter()
import argparse
import asyncio
from functools import wraps
import inspect
import hashlib
import importlib.metadata
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--rounds', type=int, default=3)
parser.add_argument('--samples', type=int, default=3)
parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
args = parser.parse_args()

if not args.worker:
    rounds = []
    for index in range(args.rounds):
        with tempfile.TemporaryDirectory(prefix='hkq-worker-') as work:
            output = Path(work) / 'facts.json'
            start = time.perf_counter()
            subprocess.run([sys.executable, __file__, '--worker', '--samples', str(args.samples),
                            '--output', str(output)], check=True)
            facts = json.loads(output.read_text())
            facts['process_wall_ms'] = (time.perf_counter() - start) * 1000
            rounds.append(facts)
    groups, outcomes = {}, {}
    for facts in rounds:
        for call in facts['calls']:
            groups.setdefault(call['workload'], []).append(call['latency_ms'])
            key = call['workload'] + ':' + ','.join(call['outcomes'])
            outcomes.setdefault(key, []).append(call['latency_ms'])
    def distribution(values):
        ordered = sorted(values)
        return {'n': len(values), 'min_ms': min(values), 'median_ms': statistics.median(values),
                'p95_nearest_rank_ms': ordered[__import__('math').ceil(.95 * len(ordered)) - 1],
                'max_ms': max(values)}
    args.output.write_text(json.dumps({'schema': 1, 'scope': 'local sandbox, not deployment',
        'predeclared_rounds': args.rounds, 'predeclared_warm_samples_per_round': args.samples,
        'budget_bytes': 65536, 'query_id': 'version2-retry-limit-warning',
        'distributions': {k: distribution(v) for k, v in groups.items()},
        'outcome_distributions': {k: distribution(v) for k, v in outcomes.items()},
        'coordinator_argv': sys.argv, 'rounds': rounds}, indent=2))
    print(args.output)
    raise SystemExit

from aiohttp import web
import psutil
from patchright.async_api import Page, BrowserContext, Browser as NativeBrowser
import release_check as gate
import vresto.browser as browser_module
import vresto.server as server_module
from vresto.acquisition import Acquisition
from vresto.cache import DocumentCache
from vresto.discovery import Discovery
IMPORT_MS = (time.perf_counter() - BOOT) * 1000

spans = []
browser_versions = set()
active_call = 'startup'
# Real HTTP does not propagate client ContextVars to MCP handler tasks. This
# benchmark dispatches ordinary workloads serially: capture the active numeric
# call at stage entry. Concurrent queue calls deliberately share 'queue' scope,
# never a fabricated per-client attribution; their outcomes are paired below.
def instrument(owner, name, phase):
    original = getattr(owner, name)
    def record(start, call_id, label=phase):
        spans.append({'call': call_id, 'phase': label,
                      'duration_ms': (time.perf_counter() - start) * 1000})
    if inspect.iscoroutinefunction(original):
        @wraps(original)
        async def wrapped(*a, **kw):
            start = time.perf_counter()
            call_id = active_call
            label = ('browser_ensure_ready' if phase == 'browser_startup' and a[0].browser is not None else phase)
            try:
                result = await original(*a, **kw)
                if phase == 'browser_startup' and a[0].browser is not None:
                    browser_versions.add(a[0].browser.version)
                    assert a[0].browser.version == '153.0.8010.12', 'browser revision mismatch'
                return result
            finally:
                record(start, call_id, label)
    else:
        @wraps(original)
        def wrapped(*a, **kw):
            start = time.perf_counter()
            call_id = active_call
            try:
                return original(*a, **kw)
            finally:
                record(start, call_id)
    setattr(owner, name, wrapped)

class AdmissionLock(asyncio.Lock):
    async def acquire(self):
        start = time.perf_counter()
        call_id = active_call
        try:
            return await super().acquire()
        finally:
            spans.append({'call': call_id, 'phase': 'admission',
                          'duration_ms': (time.perf_counter() - start) * 1000})

original_init = Acquisition.__init__
def acquisition_init(self, *a, **kw):
    original_init(self, *a, **kw)
    self.lock = AdmissionLock()
Acquisition.__init__ = acquisition_init
for owner, name, phase in [
    (browser_module.Browser, 'start', 'browser_startup'),
    (Page, 'goto', 'navigation'),
    (NativeBrowser, 'new_context', 'context_startup'),
    (BrowserContext, 'new_page', 'page_startup'),
    (BrowserContext, 'close', 'context_cleanup'),
    (Page, 'evaluate', 'native_probe'),
    (Page, 'wait_for_load_state', 'render_load_state'),
    (Page, 'content', 'dom_serialization'),
    (browser_module.Browser, '_settled_response', 'challenge_revalidation'),
    (browser_module.Browser, '_settle_rendered', 'render_quiet'),
    (browser_module, 'expand_content', 'native_expansion'),
    (browser_module, 'extract', 'extraction'),
    (server_module, 'select_highlights', 'selection'),
    (server_module, 'expand_evidence', 'evidence_expansion'),
    (server_module, 'safe_sources', 'serialization_redaction'),
    (server_module, 'apply_budget', 'serialization_budget'),
    (server_module, 'tool_result', 'serialization_result'),
    (DocumentCache, 'lookup', 'cache_lookup'),
    (DocumentCache, 'store', 'cache_store'),
    (Discovery, 'search', 'discovery_admission_execution'),
]:
    instrument(owner, name, phase)
# PDF parser is asynchronous and includes its isolated helper process.
import vresto.acquisition as acquisition_module
instrument(acquisition_module, 'parse_pdf', 'normalization_pdf')

QUERY = 'Version 2 retry_limit cancellation warning legacy mode'
CORPUS = ('docs', 'discussion', 'js', 'document.pdf')
BASE = 'http://127.0.0.1:18081/'
LAZY_BODY = '<article><h2>Bob replied</h2><p>Version 2 only, not Version 1. Do not swallow CancelledError without uncancel.</p></article>'
PENDING_BODY = 'Warning: do not enable legacy mode. Cancellation must propagate.'

def fixture_html(kind):
    if kind == 'docs':
        sections = ''.join(f'<section><h2>Operation {i}</h2><p>Version 2 operation {i} uses retry_limit = 0. '
            'Warning: do not enable legacy mode. Cancellation must propagate; never swallow CancelledError '
            'without uncancel. This requirement applies only to Version 2, not Version 1.</p>'
            '<pre><code>retry_limit = 0\nasync def operation():\n    await task()</code></pre>'
            '<table><tr><th>Option</th><th>Default</th></tr><tr><td>retry_limit</td><td>0</td></tr></table></section>'
            for i in range(32))
        return '<nav><a href="/menu">Unrelated menu</a></nav><main><h1>Version 2 documentation</h1>' + sections + '</main><footer><a href="/help">Help</a><a href="/about">About</a></footer>'
    if kind == 'discussion':
        return '''<main><h1>Version 2 discussion</h1><article><h2>Alice commented</h2>
        <p>retry_limit = 0. Warning: do not enable legacy mode.</p></article>
        <details><summary>Details</summary><p>Cancellation must propagate; never swallow CancelledError without uncancel.</p></details>
        <button type="button" aria-controls="replies" onclick="fetch('/lazy').then(r=>r.text()).then(t=>{document.querySelector('#replies').innerHTML=t;this.remove()})">Load more comments</button>
        <div id="replies"></div></main>'''
    return '''<main><h1>Version 2 rendered documentation</h1><p>retry_limit = 0.</p><div id="pending"></div><div id="timer"></div></main>
        <script>fetch('/pending').then(r=>r.text()).then(t=>document.querySelector('#pending').textContent=t);
        setTimeout(()=>document.querySelector('#timer').textContent='Timer qualification: Version 2 only, not Version 1.',750);</script>'''

async def run():
    calls, samples, observed, visits = [], [], {}, []
    entered, release = asyncio.Event(), asyncio.Event()
    async def page(request):
        visits.append(request.path)
        assert 'fixture-session' not in str(request.headers) and 'fixture-access' not in str(request.headers)
        if request.path == '/hold':
            entered.set()
            await release.wait()
            return web.Response(status=404)
        if request.path == '/document.pdf':
            return web.Response(content_type='application/pdf', body=gate.pdf_fixture())
        if request.path == '/lazy':
            await asyncio.sleep(.12)
            return web.Response(text=LAZY_BODY)
        if request.path == '/pending':
            await asyncio.sleep(.18)
            return web.Response(text=PENDING_BODY)
        return web.Response(content_type='text/html', text=fixture_html(request.path.strip('/')))
    fixture = web.Application()
    fixture.router.add_get('/{path:.*}', page)
    runner = web.AppRunner(fixture)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18081).start()
    async def sample():
        while True:
            samples.append((time.perf_counter(), gate.rss()))
            for process in gate.processes():
                try:
                    observed[process.pid] = process.create_time()
                except psutil.NoSuchProcess:
                    pass
            await asyncio.sleep(.02)
    sampler = asyncio.create_task(sample())
    facts = {'imports_ms': IMPORT_MS, 'rss_imported_idle_bytes': gate.rss(),
             'python': sys.version.split()[0], 'sample_interval_ms': 20,
             'logical_cpus_not_entitlement': psutil.cpu_count(), 'calls': calls,
             'dependencies': {name: importlib.metadata.version(name) for name in
                              ('markdownify', 'lxml', 'patchright', 'mcp', 'pypdf')},
             'corpus_sha256': {kind: hashlib.sha256(gate.pdf_fixture() if kind == 'document.pdf'
                                else fixture_html(kind).encode()).hexdigest() for kind in CORPUS},
             'fixture_cli_release': 'kagi 0.23.0 (synthetic, not upstream discovery)'}
    facts['corpus_sha256'].update(lazy=hashlib.sha256(LAZY_BODY.encode()).hexdigest(),
                                 pending=hashlib.sha256(PENDING_BODY.encode()).hexdigest())
    source_root = Path(acquisition_module.__file__).resolve().parent
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_root.glob('*.py'))}
    facts['provenance'] = {
        'source_base_revision': '3650898d0339c59171bdd120d18e3f171684cdf7', 'source_import_root': str(source_root),
        'source_files_sha256': files,
        'source_tree_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'release_harness_sha256': hashlib.sha256(Path(gate.__file__).read_bytes()).hexdigest(),
        'uv_lock_sha256': hashlib.sha256(Path(__file__).resolve().parent.parent.joinpath('uv.lock').read_bytes()).hexdigest(),
        'worker_argv': sys.argv, 'python_executable': sys.executable,
        'clock': 'perf_counter monotonic wall; bootstrap excludes interpreter initialization',
        'serialization_scope': 'redaction, budget and CallToolResult only; SDK/HTTP transport not isolated',
        'idle_scope': 'Clock.advance simulates idle expiry; not a real one-hour wait',
        'queue_scope': 'held episodes with one active and two contenders; all complete after release within the shared request deadline',
        'rss_scope': 'Python client/server/fixture and recursive descendants; shared pages double-counted; 20ms samples; coordinator excluded',
    }
    async def call(session, workload, tool, arguments):
        global active_call
        index = len(calls)
        row = {'workload': workload, 'index': index}
        calls.append(row)
        active_call = 'queue' if workload.startswith('queue_') else index
        start, initial_visits = time.perf_counter(), len(visits)
        try:
            reply = await session.call_tool(tool, arguments)
            result = gate.data(reply)
            completed = time.perf_counter()
            wire_size = len(reply.model_dump_json(exclude_none=True).encode())
            assert wire_size <= 65536, 'shared output budget exceeded'
            sources = result.get('sources', result.get('results', []))
            row.update(latency_ms=(completed - start) * 1000,
                serialized_mcp_result_bytes=wire_size,
                returned_highlight_counts=[len(s.get('highlights', [])) for s in sources],
                omitted_highlight_counts=[s.get('omitted_highlight_count', 0) for s in sources],
                truncated_field_counts=[len(s.get('truncated_fields', [])) for s in sources],
                outcomes=[s.get('error', {}).get('code', 'ok') for s in sources],
                cache_states=[s.get('cache_state') for s in sources],
                versions=[s.get('content_version') for s in sources],
                fixture_visits=len(visits) - initial_visits,
                rss_at_completion_bytes=gate.rss(),
                budget=result.get('budget'),
                coverage=[s.get('coverage') for s in sources])
            peak = [v for t, v in samples if start <= t <= time.perf_counter()]
            row['sampled_active_peak_rss_bytes'] = max(peak, default=gate.rss())
            return sources
        finally:
            if not workload.startswith('queue_'):
                active_call = 'idle'
    try:
        with tempfile.TemporaryDirectory(prefix='hkq-cache-') as work:
            work = Path(work)
            cli = work / 'cli.py'
            rows = [{'t': 0, 'rank': i + 1, 'title': kind, 'url': BASE + kind, 'snippet': 'Provider summary'}
                    for i, kind in enumerate(CORPUS)]
            cli.write_text('import json,sys\nif "--version" in sys.argv: print("kagi 0.23.0")\nelse: print(' + repr(json.dumps({'data': rows})) + ')\n')
            start = time.perf_counter()
            clock = gate.Clock()
            # Keep the historical 64 KiB performance workload via operator settings,
            # not a model-facing per-call budget.
            async with gate.server(work / 'cache', cli, clock, response_budget_bytes=65536) as url:
                facts['server_construction_startup_ms'] = (time.perf_counter() - start) * 1000
                async with gate.client(url) as first, gate.client(url) as second:
                    facts['mcp_initialization_ms'] = (time.perf_counter() - start) * 1000 - facts['server_construction_startup_ms']
                    # MCP initialization starts warmup asynchronously. First fetch
                    # is still the cold lifetime and records actual admission/startup spans.
                    cold = await call(first, 'cold_browser_docs', 'fetch', {'urls': [BASE + 'docs'], 'freshness': 'fresh_only'})
                    assert 'retry_limit = 0' in cold[0]['content'] and 'legacy mode' in cold[0]['content']
                    await gate.eventually(lambda: len(gate.browsers()) == 1)
                    facts['rss_browser_warm_idle_bytes'] = gate.rss()
                    for repeat in range(args.samples):
                        evidence = []
                        expected_quotes = []
                        versions = {}
                        for kind in CORPUS:
                            full = await call(first, 'warm_fresh_' + kind, 'fetch', {'urls': [BASE + kind], 'freshness': 'fresh_only'})
                            assert 'error' not in full[0], full[0].get('error')
                            text = full[0]['content']
                            if kind == 'docs':
                                assert 'Unrelated menu' not in text, 'navigation retained'
                                assert 'Operation 31' in text, f'last operation absent; canonical chars={len(text)}'
                                assert 'without uncancel' in text, 'qualification absent'
                            elif kind == 'discussion':
                                assert 'Alice' in text and 'Bob' in text and 'without uncancel' in text
                                assert not full[0]['coverage']['partial']
                            elif kind == 'js':
                                assert 'Timer qualification' in text and 'Cancellation must propagate' in text
                            else:
                                assert 'Never disable verification.' in text and full[0]['document_type'] == 'pdf'
                            versions[kind] = full[0]['content_version']
                            hits = await call(first, 'cache_ordinary_' + kind, 'fetch',
                                {'urls': [BASE + kind], 'freshness': 'ordinary', 'query': QUERY})
                            assert hits[0]['cache_state'] == 'hit' and calls[-1]['fixture_visits'] == 0
                            assert hits[0]['content_version'] == versions[kind] and hits[0]['highlights']
                            evidence.append(hits[0]['highlights'][0]['evidence_id'])
                            expected_quotes.append(hits[0]['highlights'][0])
                        await call(first, 'discovery_only', 'search', {'query': QUERY, 'content_mode': 'discovery', 'quick_answer': False})
                        for freshness in ('fresh_only', 'ordinary'):
                            composed = await call(first, 'composed_' + freshness, 'search', {'query': QUERY, 'freshness': freshness, 'content_mode': 'highlights', 'quick_answer': False})
                            assert len(composed) == 4 and all('error' not in s for s in composed)
                            assert all(s.get('highlights') or s.get('omitted_highlight_count') for s in composed), 'missing evidence without explicit budget omission'
                            assert all(s['content_version'] == versions[k] for k, s in zip(CORPUS, composed))
                            if freshness == 'ordinary':
                                assert calls[-1]['fixture_visits'] == 0
                        expanded = await call(second, 'evidence_expansion', 'expand', {'evidence_ids': evidence})
                        assert calls[-1]['fixture_visits'] == 0
                        assert [s['highlights'] for s in expanded] == [[quote] for quote in expected_quotes]
                        # Keep one active fixture blocked throughout concurrent
                        # contenders; never use a sleep stagger as queue admission.
                        entered.clear()
                        release.clear()
                        active = asyncio.create_task(call(first, 'queue_active', 'fetch', {'urls': [BASE + 'hold'], 'freshness': 'fresh_only'}))
                        await asyncio.wait_for(entered.wait(), 5)
                        contenders = [asyncio.create_task(call(second, 'queue_contender', 'fetch',
                            {'urls': [BASE + 'hold'], 'freshness': 'fresh_only'})) for _ in range(2)]
                        await asyncio.sleep(.1)
                        assert not any(task.done() for task in contenders)
                        release.set()
                        results = await asyncio.gather(active, *contenders)
                        assert all(s[0]['error']['code'] == 'fetch_failed' for s in results)
                    clock.advance()
                    await gate.eventually(lambda: not gate.processes())
                    facts['rss_after_idle_teardown_bytes'] = gate.rss()
            facts['rss_after_shutdown_bytes'] = gate.rss()
        await gate.eventually(lambda: not gate.processes())
        alive = []
        for pid, created in observed.items():
            try:
                p = psutil.Process(pid)
                if p.create_time() == created and p.status() != psutil.STATUS_ZOMBIE:
                    alive.append(pid)
            except psutil.NoSuchProcess:
                pass
        assert not alive, 'surviving observed descendant'
        facts['cleanup_surviving_live_descendants'] = len(alive)
        facts['observed_descendants'] = len(observed)
    finally:
        release.set()
        await runner.cleanup()
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    facts['browser_versions'] = sorted(browser_versions)
    facts['spans'] = spans
    facts['sampled_round_peak_rss_bytes'] = max(v for _, v in samples)
    args.output.write_text(json.dumps(facts, indent=2))

asyncio.run(run())
