"""Image entrypoint seam: X server startup/secret isolation and SIGTERM cleanup.

The Xvfb executable fixture is a real child process, not a browser substitute;
native browser-mode behavior is separately checked through the public MCP fetch.
"""
import asyncio
import os
from pathlib import Path
import socket
import sys

import httpx
import psutil
import pytest


@pytest.mark.parametrize('mode', ['help', 'terminate', 'display_failure'])
async def test_headful_entrypoint_owns_display_without_discovery_credentials(tmp_path, mode):
    display = tmp_path / 'Xvfb'
    pidfile = tmp_path / 'display.pid'
    display.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
assert 'KAGI_SESSION_TOKEN' not in os.environ
assert 'KAGI_CRAWLER_ACCESS_TOKEN' not in os.environ
Path({str(pidfile)!r}).write_text(str(os.getpid()))
if {mode!r} == 'display_failure':
    sys.exit(1)
fd = int(sys.argv[sys.argv.index('-displayfd') + 1])
os.write(fd, b'42\\n')
os.close(fd)
time.sleep(60)
''')
    display.chmod(0o700)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    args = ['--host', '127.0.0.1', '--port', str(port)] if mode == 'terminate' else ['--help']
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ.get('PATH', ''),
           'KAGI_CRAWLER_ACCESS_TOKEN': 'fixture-access', 'KAGI_SESSION_TOKEN': 'fixture-session',
           'KAGI_CRAWLER_CACHE_DIRECTORY': str(tmp_path / 'cache')}
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'kagi_crawler.headful',
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        if mode == 'terminate':
            async with httpx.AsyncClient(trust_env=False) as http:
                async with asyncio.timeout(10):
                    while True:
                        assert process.returncode is None
                        try:
                            response = await http.get(f'http://127.0.0.1:{port}/health',
                                headers={'Authorization': 'Bearer fixture-access'})
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(.05)
                assert response.status_code == 200
            process.terminate()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == {'help': 0, 'terminate': 143, 'display_failure': 1}[mode]
        assert pidfile.exists()
        assert not psutil.pid_exists(int(pidfile.read_text()))
        assert b'fixture-session' not in stdout + stderr
        assert b'fixture-access' not in stdout + stderr
        if mode == 'display_failure':
            assert b'Xvfb startup failed' in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
