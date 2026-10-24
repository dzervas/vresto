"""Deployment seam: invoke the installed application's entry point over TCP."""
import asyncio
import os
import socket
import sys

import httpx
import pytest
from aiohttp import web
from test_fetch import site
from test_http import running, client, body


def startup_environment(tmp_path):
    # Only runtime paths/configuration cross into the worker, never upstream
    # credentials or crawler overrides. All authentication here is synthetic.
    env = {key: os.environ[key] for key in (
        'PATH', 'HOME', 'TMPDIR', 'LD_LIBRARY_PATH', 'FONTCONFIG_FILE',
        'PLAYWRIGHT_NODEJS_PATH', 'PLAYWRIGHT_BROWSERS_PATH', 'PYTHONPATH',
    ) if key in os.environ}
    return {**env, 'VRESTO_ACCESS_TOKEN': 'fixture-access',
            'KAGI_SESSION_TOKEN': 'fixture-session',
            'VRESTO_CACHE_DIRECTORY': str(tmp_path)}


async def test_operator_can_bind_container_interface_without_exposing_credentials(tmp_path):
    sock = socket.socket()
    sock.bind(('127.0.0.2', 0))
    port = sock.getsockname()[1]
    sock.close()
    env = startup_environment(tmp_path)
    process = await asyncio.create_subprocess_exec(sys.executable, '-c',
        'from vresto.server import main; main()', '--host', '127.0.0.2', '--port', str(port),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            async with asyncio.timeout(15):
                while True:
                    if process.returncode is not None:
                        raise AssertionError('entry point exited before accepting HTTP')
                    try:
                        response = await http.get(f'http://127.0.0.2:{port}/health')
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(.05)
            assert response.status_code == 200
            assert response.json() == {'status': 'ok'}
            response = await http.get(f'http://127.0.0.2:{port}/health',
                headers={'Authorization': 'Bearer fixture-access'})
            assert response.status_code == 200
            health = response.json()
            assert health['status'] == 'ok' and health['browser'] in ('stopped', 'starting')
            assert health['cache']['cap_bytes'] > 0 and 'fixture' not in response.text
    finally:
        if process.returncode is None:
            process.terminate()
        stdout, stderr = await process.communicate()
    assert b'fixture-session' not in stdout + stderr
    assert b'fixture-access' not in stdout + stderr


@pytest.mark.parametrize('override', [None, '1.5'])
async def test_main_launch_expansion_default_and_explicit_override_over_http(tmp_path, override):
    async def handler(request):
        if request.path == '/continuation':
            # A native associated response exceeds the old shipped 2s cap,
            # but fits the documented 10s expansion bound.
            await asyncio.sleep(3)
            return web.Response(text='Warning: delayed cancellation qualification.')
        return web.Response(content_type='text/html', text='''<main><h1>Cancellation discussion</h1>
        <section id="comments"><p>Initial public discussion.</p></section>
        <button type="button" aria-controls="comments" onclick="load()">Load more comments</button>
        <script>async function load() {
          const text = await (await fetch('/continuation')).text();
          document.querySelector('#comments').insertAdjacentHTML('beforeend', '<p>' + text + '</p>');
          document.querySelector('button').remove();
        }</script></main>''')

    async with site(handler) as (base, options):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        env = startup_environment(tmp_path)
        if override is not None:
            env['VRESTO_EXPANSION_SECONDS'] = override
        # main still parses all ordinary startup settings. The sole test adapter
        # allows the exact local fixture destination, as in other native tests;
        # it does not replace expansion settings, acquisition or HTTP transport.
        bootstrap = f'''from dataclasses import replace
from vresto import server
create_app = server.create_app
def fixture_app(settings):
    options = replace(settings.acquisition_options, fixture_destinations={options.fixture_destinations!r})
    return create_app(replace(settings, acquisition_options=options))
server.create_app = fixture_app
server.main()
'''
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', bootstrap,
            '--host', '127.0.0.1', '--port', str(port), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                async with asyncio.timeout(15):
                    while True:
                        if process.returncode is not None:
                            raise AssertionError('entry point exited before accepting HTTP')
                        try:
                            await http.get(f'http://127.0.0.1:{port}/health')
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(.05)
            async with client(f'http://127.0.0.1:{port}/mcp', token='fixture-access') as session:
                data = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))
                source = data['sources'][0]
                assert 'error' not in source, source
                if override is None:
                    assert 'Warning: delayed cancellation qualification.' in source['content']
                    assert data['acquisition_limits']['expansion_seconds'] == 10
                else:
                    assert 'Warning: delayed cancellation qualification.' not in source['content']
                    assert source['coverage']['stop_reason'] == 'duration_limit'
                    assert source['coverage']['partial']
                    assert data['acquisition_limits']['expansion_seconds'] == 1.5
        finally:
            if process.returncode is None:
                process.terminate()
            stdout, stderr = await process.communicate()
        assert b'fixture-session' not in stdout + stderr
        assert b'fixture-access' not in stdout + stderr


@pytest.mark.parametrize('value', ['0', '11', 'nan', 'invalid'])
def test_main_rejects_invalid_expansion_override(monkeypatch, tmp_path, value):
    from vresto import server
    monkeypatch.setattr(os, 'environ', startup_environment(tmp_path))
    monkeypatch.setenv('VRESTO_EXPANSION_SECONDS', value)
    monkeypatch.setattr(sys, 'argv', ['vresto'])
    with pytest.raises(ValueError):
        server.main()


async def test_operational_diagnostics_exclude_query_and_credentials(tmp_path, caplog):
    caplog.set_level('INFO', logger='vresto')
    query = 'private operator question not for logs'
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': query, 'content_mode': 'discovery', 'quick_answer': False})
        assert len(body(result)['results']) == 10
    assert 'tool=search outcome=completed elapsed_ms=' in caplog.text
    for sensitive in [query, 'client-secret', 'subscriber-secret', 'Provider summary']:
        assert sensitive not in caplog.text


@pytest.mark.parametrize('value', [None, '2', '16'])
def test_main_research_concurrency_environment(monkeypatch, tmp_path, value):
    from vresto import server
    monkeypatch.setattr(os, 'environ', startup_environment(tmp_path))
    if value is not None:
        monkeypatch.setenv('VRESTO_RESEARCH_CONCURRENCY', value)
    monkeypatch.setattr(sys, 'argv', ['vresto'])
    captured = []
    monkeypatch.setattr(server, 'create_app', lambda settings: captured.append(settings))
    monkeypatch.setattr(server.uvicorn, 'run', lambda *args, **kwargs: None)
    server.main()
    assert captured[0].research_concurrency == (int(value) if value is not None else 4)


@pytest.mark.parametrize('value', ['0', '17', '1.5', 'invalid'])
def test_main_rejects_invalid_research_concurrency(monkeypatch, tmp_path, value):
    from vresto import server
    monkeypatch.setattr(os, 'environ', startup_environment(tmp_path))
    monkeypatch.setenv('VRESTO_RESEARCH_CONCURRENCY', value)
    monkeypatch.setattr(sys, 'argv', ['vresto'])
    with pytest.raises(ValueError):
        server.main()


@pytest.mark.parametrize('value', ['0', '9', '1.5', 'invalid'])
def test_main_rejects_invalid_browser_concurrency(monkeypatch, tmp_path, value):
    from vresto import server
    monkeypatch.setattr(os, 'environ', startup_environment(tmp_path))
    monkeypatch.setenv('VRESTO_BROWSER_CONCURRENCY', value)
    monkeypatch.setattr(sys, 'argv', ['vresto'])
    with pytest.raises(ValueError):
        server.main()
