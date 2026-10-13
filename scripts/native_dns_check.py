"""Opt-in public Python docs smoke + actual native Cloudflare DoH netlog proof.

Run with the installed browser/dependencies. No discovery/Kagi calls or real
credentials; this starts only a local MCP app. Unlike release_check.py this gate
requires public DNS/HTTPS egress. Netlog instrumentation observes native traffic,
never intercepts it. Temporary logs/profile/cache are removed on completion.
"""
import asyncio
import json
from pathlib import Path
import tempfile

from patchright.async_api import BrowserType
from release_check import Clock, client, data, server


async def run():
    with tempfile.TemporaryDirectory(prefix='native-dns-gate-') as directory:
        root = Path(directory)
        netlog = root / 'netlog.json'
        original = BrowserType.launch_persistent_context
        async def observed_launch(browser_type, *args, **kwargs):
            kwargs['args'] = [*kwargs.get('args', []), f'--log-net-log={netlog}']
            return await original(browser_type, *args, **kwargs)
        BrowserType.launch_persistent_context = observed_launch
        try:
            async with server(root / 'cache', root / 'unused-cli', Clock()) as endpoint, client(endpoint) as session:
                source = data(await session.call_tool('fetch', {
                    'urls': ['https://docs.python.org/3/library/contextvars.html'],
                    'content_mode': 'full_text'}))['sources'][0]
                assert 'error' not in source, source
                assert source['document_type'] == 'html'
                assert 'Context Variables' in source['content']
        finally:
            BrowserType.launch_persistent_context = original
        log = json.loads(netlog.read_text())
        kinds = log['constants']['logEventTypes']
        events = log['events']
        configs = [e['params'] for e in events if e['type'] == kinds['DNS_CONFIG_CHANGED']]
        assert configs[-1]['secure_dns_mode'] == 2
        assert configs[-1]['doh_config']['servers'] == [{'template': 'https://1.1.1.1/dns-query'}]
        attempts = [e for e in events if e['type'] == kinds['DNS_TRANSACTION_HTTPS_ATTEMPT']]
        assert attempts, 'No native DoH transactions observed'
        request_ids = {e['source']['id'] for e in events
                       if e['type'] == kinds['DOH_URL_REQUEST']
                       and e.get('params', {}).get('hostname') == 'docs.python.org'}
        assert request_ids, 'No DNS query for the acquired document observed'
        requests = [e for e in events if e['source']['id'] in request_ids
                    and e['type'] == kinds['URL_REQUEST_START_JOB'] and 'url' in e.get('params', {})]
        assert requests and all(e['params']['url'] == 'https://1.1.1.1/dns-query' for e in requests)
        assert all(e['params']['method'] == 'POST' for e in requests)
        answers = [e for e in events if e['type'] == kinds['DNS_TRANSACTION_RESPONSE']
                   and e.get('params', {}).get('source_dependency', {}).get('id') in request_ids]
        assert answers, 'No correlated document DNS response observed'
        jobs = {e['params']['source_dependency']['id'] for e in events
                if e['source']['id'] in request_ids and e['type'] == kinds['HTTP_STREAM_REQUEST_BOUND_TO_JOB']}
        sockets = {e['params']['source_dependency']['id'] for e in events
                   if e['source']['id'] in jobs and e['type'] == kinds['SOCKET_POOL_BOUND_TO_SOCKET']}
        sessions = {e['source']['id'] for e in events
                    if e['type'] == kinds['HTTP2_SESSION_SEND_HEADERS']
                    and e.get('params', {}).get('source_dependency', {}).get('id') in jobs}
        sockets.update(e['params']['source_dependency']['id'] for e in events
                       if e['source']['id'] in sessions and e['type'] == kinds['HTTP2_SESSION_INITIALIZED'])
        cloudflare_sockets = {e['source']['id'] for e in events
                              if e['source']['id'] in sockets and e['type'] == kinds['TCP_CONNECT']
                              and e.get('params', {}).get('remote_address') == '1.1.1.1:443'}
        tls = [e for e in events if e['source']['id'] in cloudflare_sockets
               and e['type'] == kinds['SSL_CONNECT'] and 'version' in e.get('params', {})]
        assert tls and all(e['params'].get('net_error', 0) == 0 for e in tls)
        headers = [e['params'].get('headers', []) for e in events
                   if e['source']['id'] in request_ids and e['type'] == kinds['HTTP_TRANSACTION_READ_RESPONSE_HEADERS']]
        assert headers and all('HTTP/1.1 200' in h and 'content-type: application/dns-message' in h for h in headers)
        plaintext = [e for e in events if e['type'] in {
            kinds['DNS_TRANSACTION_ATTEMPT'], kinds['DNS_TRANSACTION_TCP_ATTEMPT']}]
        assert not plaintext, 'Unexpected plaintext DNS transaction'
        print(json.dumps({'python_docs_smoke': 'passed', 'document_type': source['document_type'],
            'resolved_url': source['resolved_url'], 'secure_dns_mode': configs[-1]['secure_dns_mode'],
            'cloudflare_native_doh_posts': len(requests), 'dns_responses': len(answers), 'correlated_cloudflare_tls_sessions': len(tls),
            'plaintext_dns_transactions': len(plaintext)}, sort_keys=True))


if __name__ == '__main__':
    asyncio.run(run())
