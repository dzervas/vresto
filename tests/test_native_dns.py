"""External full-browser outcomes, not accepted-switch string assertions."""
import json
from pathlib import Path
from patchright.async_api import BrowserType
from test_fetch import site
from test_http import running, client, body
from aiohttp import web


async def test_application_browser_has_effective_secure_cloudflare_dns(tmp_path, monkeypatch):
    netlog = tmp_path / 'netlog.json'
    launch = BrowserType.launch_persistent_context
    async def observed_launch(browser_type, *args, **kwargs):
        # Instrument the real external browser boundary; no fabricated responses.
        kwargs['args'] = [*kwargs.get('args', []), f'--log-net-log={netlog}']
        return await launch(browser_type, *args, **kwargs)
    monkeypatch.setattr(BrowserType, 'launch_persistent_context', observed_launch)
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Native DNS configured</h1>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'Native DNS configured' in source['content']
    log = json.loads(netlog.read_text())
    event_types = log['constants']['logEventTypes']
    configs = [event['params'] for event in log['events'] if event['type'] == event_types['DNS_CONFIG_CHANGED']]
    assert configs
    effective = configs[-1]
    assert effective['secure_dns_mode'] == 2
    assert effective['doh_config']['servers'] == [{'template': 'https://1.1.1.1/dns-query'}]
    assert effective['can_use_secure_dns_transactions'] is True


async def test_acquisition_fails_if_effective_dns_is_not_secure(tmp_path, monkeypatch):
    launch = BrowserType.launch_persistent_context
    async def overridden_launch(browser_type, profile, **kwargs):
        local_state = Path(profile, 'Local State')
        prefs = json.loads(local_state.read_text())
        prefs['dns_over_https']['mode'] = 'off'
        local_state.write_text(json.dumps(prefs))
        return await launch(browser_type, profile, **kwargs)
    monkeypatch.setattr(BrowserType, 'launch_persistent_context', overridden_launch)
    received = []
    async def handler(request):
        received.append(request.path)
        return web.Response(content_type='text/html', text='<h1>Must not acquire</h1>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert source['error']['code'] == 'browser_unavailable'
        assert received == []
