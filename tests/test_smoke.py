"""The opt-in operator script can be checked without authorized upstream access."""
import asyncio
import json
import os
from pathlib import Path
import sys

from aiohttp import web
from test_fetch import site
from test_http import running


async def test_smoke_reports_acquisition_separately_without_printing_page_bodies(tmp_path):
    async def handler(request):
        if request.path == '/unsupported':
            return web.Response(content_type='text/plain', text='never print this body')
        return web.Response(content_type='text/html', text='<h1>Version 2</h1><p>' +
            'retry_limit is zero. Never enable legacy mode. Read all warnings before deployment. ' * 10 + '</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _):
        cases = tmp_path / 'public-cases.json'
        cases.write_text(json.dumps([
            {'category': 'identifiers', 'url': base, 'query': 'retry_limit', 'expected': ['retry_limit']},
            {'category': 'technical', 'url': base + '/unsupported', 'query': 'answer', 'expected': ['answer']},
        ]))
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LD_LIBRARY_PATH',
               'PLAYWRIGHT_BROWSERS_PATH', 'PLAYWRIGHT_NODEJS_PATH') if key in os.environ}
        env['KAGI_CRAWLER_ACCESS_TOKEN'] = 'client-secret'
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).resolve().parents[1] / 'scripts/live_smoke.py'),
            '--authorized-live', '--endpoint', url, '--cases', str(cases),
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        good, bad = result['observations']
        assert good['acquisition_error'] is None
        assert good['selection_error'] is None
        assert good['acquired_expected'] == good['selected_expected'] == [True]
        assert bad['acquisition_error'] == 'unsupported_format'
        assert 'selected_expected' not in bad
        for private in [base.encode(), b'client-secret', b'subscriber-secret', b'legacy mode', b'never print this body']:
            assert private not in stdout + stderr
