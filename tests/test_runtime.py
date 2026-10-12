"""Deployment seam: invoke the installed application's entry point over TCP."""
import asyncio
import os
import socket
import sys

import httpx
from test_http import running, client, body


async def test_operator_can_bind_container_interface_without_exposing_credentials(tmp_path):
    sock = socket.socket()
    sock.bind(('127.0.0.2', 0))
    port = sock.getsockname()[1]
    sock.close()
    env = {**os.environ, 'KAGI_CRAWLER_ACCESS_TOKEN': 'fixture-access',
           'KAGI_SESSION_TOKEN': 'fixture-session',
           'KAGI_CRAWLER_CACHE_DIRECTORY': str(tmp_path)}
    process = await asyncio.create_subprocess_exec(sys.executable, '-c',
        'from kagi_crawler.server import main; main()', '--host', '127.0.0.2', '--port', str(port),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            async with asyncio.timeout(15):
                while True:
                    if process.returncode is not None:
                        raise AssertionError('entry point exited before accepting HTTP')
                    try:
                        response = await http.get(f'http://127.0.0.2:{port}/credits')
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(.05)
            assert response.status_code == 401
            response = await http.get(f'http://127.0.0.2:{port}/credits',
                headers={'Authorization': 'Bearer fixture-access'})
            assert response.status_code == 200
            assert 'software developed by UncleCode' in response.text
    finally:
        if process.returncode is None:
            process.terminate()
        stdout, stderr = await process.communicate()
    assert b'fixture-session' not in stdout + stderr
    assert b'fixture-access' not in stdout + stderr


async def test_operational_diagnostics_exclude_query_and_credentials(tmp_path, caplog):
    caplog.set_level('INFO', logger='kagi_crawler')
    query = 'private operator question not for logs'
    async with running(tmp_path) as (url, _), client(url) as session:
        result = await session.call_tool('search', {'query': query, 'content_mode': 'discovery'})
        assert len(body(result)['results']) == 10
    assert 'tool=search outcome=completed elapsed_ms=' in caplog.text
    for sensitive in [query, 'client-secret', 'subscriber-secret', 'Provider summary']:
        assert sensitive not in caplog.text
