"""Opt-in diagnostic CLI seam: actual browser outcomes, no fabricated GPU data."""
import asyncio
import json
import os
import shutil
import sys

import pytest


@pytest.mark.parametrize('headed', [False, True])
async def test_browser_diagnostic_reports_real_owned_contexts_without_credentials(headed):
    if headed and not shutil.which('Xvfb'):
        pytest.skip('real Xvfb is not installed in this test environment')
    arguments = ['--headful'] if headed else []
    env = {**os.environ, 'KAGI_CRAWLER_HEADFUL': '0',
        'KAGI_CRAWLER_BROWSER_CHANNEL': 'chromium',
        'KAGI_CRAWLER_ACCESS_TOKEN': 'diagnostic-fixture-access',
        'KAGI_SESSION_TOKEN': 'diagnostic-fixture-session'}
    process = await asyncio.create_subprocess_exec(sys.executable, '-m',
        'kagi_crawler.browser_check', *arguments, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
        assert process.returncode == 0, stderr.decode()
        report = json.loads(stdout)
        assert report['channel'] == 'chromium'
        assert report['headful'] is headed
        assert report['browser_version']
        assert report['secure_doh_verified'] is True
        for name in ('persistent_profile', 'ephemeral_context'):
            context = report[name]
            assert context['webdriver'] is False
            assert ('HeadlessChrome' in context['user_agent']) is not headed
            assert context['viewport']['width'] > 0 and context['viewport']['height'] > 0
            for kind in ('webgl', 'webgl2'):
                assert isinstance(context[kind]['available'], bool)
                assert set(context[kind]) == {'available', 'vendor', 'renderer'}
        assert b'diagnostic-fixture-access' not in stdout + stderr
        assert b'diagnostic-fixture-session' not in stdout + stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
