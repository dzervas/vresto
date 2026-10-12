from test_http import running, client, body
import pytest
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from aiohttp import web
from kagi_crawler.transport import AcquisitionOptions


@asynccontextmanager
async def site(handler):
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, '127.0.0.1', 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield f'http://127.0.0.1:{port}', AcquisitionOptions(
            fixture_destinations=(('127.0.0.1', port, '127.0.0.1'),))
    finally:
        await runner.cleanup()


@pytest.mark.parametrize('target', ['file:///etc/passwd', 'http://127.0.0.1/',
    'http://169.254.169.254/', 'http://168.63.129.16/', 'http://[::1]/',
    'http://[::ffff:127.0.0.1]/', 'http://2130706433/', 'http://0x7f000001/',
    'http://user:pass@example.org/', 'http://example.org:22/',
    'http://example.org\\@127.0.0.1/', 'http://[64:ff9b::7f00:1]/',
    'http://example.org:0/', 'http://0177.0.0.1/', 'http://127.0.0.1./',
    'http://[fe80::1%25lo]/', 'http://%31%32%37.0.0.1/', 'http://localhost/'])
async def test_fetch_rejects_forbidden_destinations(tmp_path, target):
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [target]})
        assert not result.isError
        assert body(result)['sources'][0]['error']['code'] == 'policy_rejected'
        assert 'subscriber-secret' not in result.model_dump_json()
        assert 'pass@' not in result.model_dump_json()


async def test_rendered_technical_markdown_and_source_metadata(tmp_path, caplog):
    requests = []
    async def handler(request):
        requests.append((request.path, dict(request.headers)))
        if request.path == '/api':
            return web.Response(text='JavaScript evidence')
        if request.path == '/redirect':
            raise web.HTTPFound('/docs')
        return web.Response(content_type='text/html', text='''<!doctype html>
        <title>Technical Guide</title><h1>Version 2</h1><p>Warning: do not enable legacy mode.</p>
        <pre><code class="language-python">print("safe")</code></pre>
        <table><tr><th>Option</th><th>Default</th></tr><tr><td>retry</td><td>0</td></tr></table>
        <p>Copyright Example. Licensed under Apache-2.0.</p><p id="dynamic"></p>
        <script>fetch('/api').then(r=>r.text()).then(t=>document.querySelector('#dynamic').textContent=t)</script>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [base + '/redirect']})
        data = body(result)['sources'][0]
        assert 'error' not in data, data
        assert data['requested_url'] == base + '/redirect'
        assert data['resolved_url'] == base + '/docs'
        assert data['title'] == 'Technical Guide'
        assert data['document_type'] == 'html'
        assert data['cache_state'] == 'miss'
        assert data['fetched_at'].endswith('+00:00')
        assert len(data['content_version']) == 64
        assert 'published_at' not in data
        for text in ['# Version 2', 'Warning: do not enable legacy mode.', 'print("safe")',
                     'Option', 'retry', 'Apache-2.0', 'JavaScript evidence']:
            assert text in data['content']
        assert '```' in data['content'] and '|' in data['content']
        assert [p for p, _ in requests].count('/docs') == 1
        assert '/api' in [p for p, _ in requests]
        assert 'subscriber-secret' not in str(requests) + result.model_dump_json() + caplog.text
        assert 'client-secret' not in str(requests) + result.model_dump_json() + caplog.text


async def test_redirect_and_browser_subrequests_never_reach_forbidden_site(tmp_path):
    forbidden = []
    async def sink(request):
        forbidden.append(request.path)
        return web.Response(text='private secret')
    async with site(sink) as (private, _):
        async def upstream(request):
            if request.path == '/redirect':
                raise web.HTTPFound(private + '/redirect-target')
            return web.Response(content_type='text/html', text=f'''<h1>Untrusted</h1>
            <iframe src="{private}/frame"></iframe><script>
            fetch('{private}/fetch').catch(()=>{{}});
            window.open('{private}/popup');
            </script>''')
        async with site(upstream) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            result = body(await session.call_tool('fetch', {'urls': [base + '/redirect', base + '/js']}))
            assert all(s['error']['code'] == 'policy_rejected' for s in result['sources'])
            assert forbidden == []


async def test_mixed_dns_answers_and_rebinding_are_rejected_before_connect(tmp_path):
    requests = []
    async def handler(request):
        requests.append(request.path)
        return web.Response(content_type='text/html', text="<h1>DNS</h1><script>fetch('/forbidden')</script>")
    async with site(handler) as (base, options):
        port = int(base.rsplit(':', 1)[1])
        calls = 0
        async def lookup(host, _):
            nonlocal calls
            calls += 1
            return ['127.0.0.1'] if calls == 1 else ['127.0.0.1', '127.0.0.2']
        options = replace(options, fixture_destinations=(('fixture.test', port, '127.0.0.1'),), dns_lookup=lookup)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            result = body(await session.call_tool('fetch', {'urls': [f'http://fixture.test:{port}/first', f'http://fixture.test:{port}/second']}))
            assert all(s['error']['code'] == 'policy_rejected' for s in result['sources'])
            assert requests == ['/first']


@pytest.mark.parametrize('kind,code', [('gzip', 'resource_limit'), ('chunked', 'resource_limit'),
    ('rate', 'rate_limited'), ('challenge', 'blocked'), ('pdf', 'unsupported_format'),
    ('missing', 'fetch_failed'), ('slow', 'fetch_timeout')])
async def test_transport_failures_are_distinct_and_bounded(tmp_path, kind, code):
    import gzip
    async def handler(request):
        if kind == 'gzip':
            return web.Response(body=gzip.compress(b'x' * 100000), headers={'Content-Encoding': 'gzip', 'Content-Type': 'text/html'})
        if kind == 'chunked':
            r = web.StreamResponse(headers={'Content-Type': 'text/html'})
            await r.prepare(request)
            try:
                for _ in range(30):
                    await r.write(b'x' * 1000)
            except ConnectionError:
                pass
            return r
        if kind == 'slow':
            await asyncio.sleep(0.5)
        if kind == 'rate':
            return web.Response(status=429)
        if kind == 'challenge':
            return web.Response(content_type='text/html', text='<title>Just a moment...</title>')
        if kind == 'pdf':
            return web.Response(content_type='application/pdf', body=b'%PDF-1.7')
        return web.Response(status=404)
    async with site(handler) as (base, options):
        options = replace(options, response_bytes=4096, request_timeout=0.1)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            result = body(await session.call_tool('fetch', {'urls': [base]}))
            assert result['sources'][0]['error']['code'] == code


async def test_service_credentials_in_input_are_not_sent_or_echoed(tmp_path, caplog):
    received = []
    async def handler(request):
        received.append(str(request.rel_url))
        return web.Response(content_type='text/html', text='<h1>Public</h1>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [base + '/?token=subscriber-secret']})
        assert result.isError
        assert received == []
        assert 'subscriber-secret' not in result.model_dump_json() + caplog.text


def browser_pids():
    import psutil
    result = set()
    for p in psutil.Process().children(recursive=True):
        try:
            if 'chrome' in p.name() and p.parent().name() == 'node':
                result.add(p.pid)
        except psutil.NoSuchProcess:
            pass
    return result


async def test_application_browser_shared_across_clients_and_released_at_shutdown(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Shared browser</h1><p>' + 'Public technical documentation. ' * 20 + '</p>')
    before = browser_pids()
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _):
        async with client(url) as first, client(url) as second:
            results = await asyncio.gather(first.call_tool('fetch', {'urls': [base]}), second.call_tool('fetch', {'urls': [base]}))
            assert all('error' not in body(r)['sources'][0] for r in results), [body(r) for r in results]
            warm = browser_pids() - before
            assert len(warm) == 1
            again = body(await first.call_tool('fetch', {'urls': [base]}))
            assert 'error' not in again['sources'][0]
            assert browser_pids() - before == warm
    assert browser_pids() == before


async def test_mixed_success_failure_preserves_document(tmp_path):
    async def handler(request):
        if request.path == '/missing':
            return web.Response(status=404)
        return web.Response(content_type='text/html', text='<title>Successful</title><h1>Evidence</h1><p>' + 'Version 2 preserves warnings and attribution. ' * 20 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        sources = body(await session.call_tool('fetch', {'urls': [base, base + '/missing']}))['sources']
        assert 'Evidence' in sources[0]['content']
        assert sources[1]['error']['code'] == 'fetch_failed'
        assert sources[1]['requested_url'] == base + '/missing'


async def test_cancelled_render_closes_downloads_browser_and_keeps_mcp_usable(tmp_path):
    from mcp import types
    entered = asyncio.Event()
    disconnected = asyncio.Event()
    async def handler(request):
        if request.path == '/hang':
            entered.set()
            r = web.StreamResponse(headers={'Content-Type': 'text/plain'})
            await r.prepare(request)
            try:
                while True:
                    await r.write(b'x')
                    await asyncio.sleep(0.02)
            except ConnectionError:
                disconnected.set()
            return r
        if request.path == '/slow':
            return web.Response(content_type='text/html', text="<h1>Slow</h1><script>fetch('/hang')</script>")
        return web.Response(content_type='text/html', text='<h1>Recovered</h1><p>' + 'Public technical documentation. ' * 20 + '</p>')
    before = browser_pids()
    calls = []
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, calls=calls) as session:
        task = asyncio.create_task(session.call_tool('fetch', {'urls': [base + '/slow']}))
        await asyncio.wait_for(entered.wait(), 10)
        await session.send_notification(types.ClientNotification(types.CancelledNotification(
            method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=calls[-1]))))
        try:
            await task
        except Exception as exc:
            assert 'cancel' in str(exc).lower()
        async with asyncio.timeout(5):
            await disconnected.wait()
            while browser_pids() != before:
                await asyncio.sleep(0.02)
        recovered = body(await session.call_tool('fetch', {'urls': [base + '/ok']}))
        assert 'Recovered' in recovered['sources'][0]['content']
    assert browser_pids() == before


@pytest.mark.parametrize('kind', ['service_worker', 'websocket'])
async def test_service_worker_and_websocket_paths_cannot_bypass_transport(tmp_path, kind):
    forbidden = []
    async def sink(request):
        forbidden.append(request.path)
        return web.Response(text='private')
    async with site(sink) as (private, _):
        async def handler(request):
            if request.path == '/sw.js':
                return web.Response(content_type='application/javascript', text=f"fetch('{private}/worker')")
            script = "navigator.serviceWorker.register('/sw.js').catch(()=>{});" if kind == 'service_worker' else f"new WebSocket('{private.replace('http:', 'ws:')}/socket');"
            return web.Response(content_type='text/html', text=f"<h1>Worker test</h1><p>{'Public documentation. ' * 30}</p><script>{script}</script>")
        async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            data = body(await session.call_tool('fetch', {'urls': [base]}))
            source = data['sources'][0]
            if kind == 'service_worker' or 'error' in source:
                assert source['error']['code'] in ('rendering_unsupported', 'policy_rejected')
            else:
                # Patchright may suppress the socket before reporting its route;
                # the externally meaningful safety assertion is zero handshakes.
                assert 'Worker test' in source['content']
            assert forbidden == []


async def test_queue_capacity_and_wait_are_bounded_across_clients(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return web.Response(status=404)
    async with site(handler) as (base, options):
        options = replace(options, queue_capacity=1, queue_wait=0.1)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as first, client(url) as second:
            active = asyncio.create_task(first.call_tool('fetch', {'urls': [base]}))
            await asyncio.wait_for(entered.wait(), 3)
            queued = asyncio.create_task(second.call_tool('fetch', {'urls': [base]}))
            await asyncio.sleep(0.03)
            excess = body(await second.call_tool('fetch', {'urls': [base]}))
            assert excess['sources'][0]['error']['code'] == 'queue_full'
            assert body(await queued)['sources'][0]['error']['code'] == 'queue_timeout'
            release.set()
            assert body(await active)['sources'][0]['error']['code'] == 'fetch_failed'


@pytest.mark.parametrize('arguments', [{'urls': []}, {'urls': ['https://example.org'] * 11},
    {'urls': ['https://example.org'], 'headers': {'Authorization': 'fake'}},
    {'urls': ['https://example.org'], 'content_mode': 'highlights'}])
async def test_fetch_only_advertises_implemented_inputs(tmp_path, arguments):
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', arguments)
        assert result.isError
        assert body(result)['error']['code'] == 'invalid_input'


async def test_aggregate_headers_are_bounded_before_body_acquisition(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Not evidence</h1>',
                            headers={f'X-Header-{i}': 'x' * 1000 for i in range(40)})
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base]}))
        assert data['sources'][0]['error']['code'] == 'resource_limit'


async def test_driver_receives_only_allowlisted_environment(tmp_path, monkeypatch):
    import os
    import sys
    import patchright
    from pathlib import Path
    node = os.environ.get('PLAYWRIGHT_NODEJS_PATH') or str(Path(patchright.__file__).parent / 'driver' / 'node')
    marker = tmp_path / 'driver-started'
    wrapper = tmp_path / 'assert-node'
    wrapper.write_text(f'''#!{sys.executable}
import os, sys
assert os.environ.get('KAGI_SESSION_TOKEN') is None
assert os.environ.get('KAGI_CRAWLER_ACCESS_TOKEN') is None
assert os.environ.get('KAGI_API_KEY') is None
open({str(marker)!r}, 'w').close()
os.execv({node!r}, [{node!r}, *sys.argv[1:]])
''')
    wrapper.chmod(0o700)
    monkeypatch.setenv('PLAYWRIGHT_NODEJS_PATH', str(wrapper))
    monkeypatch.setenv('KAGI_SESSION_TOKEN', 'subscriber-secret')
    monkeypatch.setenv('KAGI_CRAWLER_ACCESS_TOKEN', 'client-secret')
    monkeypatch.setenv('KAGI_API_KEY', 'unused-fake-api-key')
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Isolated browser</h1><p>' + 'Public page evidence. ' * 30 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base]}))
        assert 'Isolated browser' in data['sources'][0]['content']
        assert marker.exists()


async def test_render_navigation_redirect_is_explicit_not_wrong_source_metadata(tmp_path):
    requests = []
    async def handler(request):
        requests.append(request.path)
        return web.Response(content_type='text/html', text='<h1>Redirecting</h1><script>location.href="/next"</script>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base]}))
        assert data['sources'][0]['error']['code'] == 'rendering_unsupported'
        assert requests == ['/']


@pytest.mark.parametrize('bound', ['dom', 'requests', 'aggregate_bytes'])
async def test_rendered_page_resource_limits_are_explicit(tmp_path, bound):
    async def handler(request):
        if request.path == '/data':
            return web.Response(text='x' * 2048)
        script = "document.body.append('x'.repeat(10000))" if bound == 'dom' else "for(let i=0;i<6;i++) fetch('/data?i='+i);"
        return web.Response(content_type='text/html', text=f"<h1>Bounded</h1><p>{'Public documentation. ' * 20}</p><script>{script}</script>")
    async with site(handler) as (base, options):
        options = replace(options, response_bytes=4096,
                          max_requests=2 if bound == 'requests' else 64,
                          job_bytes=3000 if bound == 'aggregate_bytes' else 8 * 1024 * 1024)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            data = body(await session.call_tool('fetch', {'urls': [base]}))
            assert data['sources'][0]['error']['code'] == 'resource_limit'


async def test_rate_limit_backoff_does_not_repeat_upstream_requests(tmp_path):
    received = []
    async def handler(request):
        received.append(request.path)
        return web.Response(status=429)
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        for _ in range(2):
            data = body(await session.call_tool('fetch', {'urls': [base]}))
            assert data['sources'][0]['error']['code'] == 'rate_limited'
        assert received == ['/']
