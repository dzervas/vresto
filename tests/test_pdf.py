from test_http import running, client, body
from test_fetch import site
from aiohttp import web
import asyncio
from dataclasses import replace
import pytest


def pdf_bytes(pages):
    # Tiny literal PDF fixture: independent text objects, no model/OCR generator.
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'']
    kids = []
    for text in pages:
        number = len(objects) + 1
        kids.append(f'{number} 0 R')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {number + 1} 0 R >>'.encode())
        stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode()
        objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream')
    objects[1] = f'<< /Type /Pages /Count {len(pages)} /Kids [{" ".join(kids)}] >>'.encode()
    data = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    position = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    return data + f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{position}\n%%EOF\n'.encode()


async def test_pdf_full_text_page_citations_highlights_and_cache(tmp_path):
    requests = []
    async def handler(request):
        requests.append(request.path)
        if request.path == '/redirect':
            raise web.HTTPFound('/document')
        return web.Response(content_type='application/pdf', body=pdf_bytes([
            'Version 2 connect_safe requires TLS. Never disable verification.',
            'Compatibility: legacy mode is not supported.']))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base + '/redirect']}))['sources'][0]
        assert 'error' not in source, source
        assert source['document_type'] == 'pdf'
        assert source['resolved_url'] == base + '/document'
        assert '# PDF page 1' in source['content'] and '# PDF page 2' in source['content']
        assert 'Never disable verification.' in source['content']
        assert source['pdf']['total_pages'] == 2
        selected = body(await session.call_tool('fetch', {'urls': [base + '/redirect'], 'query': 'connect_safe'}))['sources'][0]
        quote = selected['highlights'][0]
        assert quote['pages'] == [1]
        assert quote['text'] == source['content'][quote['start']:quote['end']]
        assert quote['evidence_id'].startswith('p1.')
        assert selected['cache_state'] == 'hit'
        assert requests == ['/redirect', '/document']


async def test_search_discovers_pdf_and_preserves_verbatim_page_evidence(tmp_path):
    import json
    async def handler(request):
        return web.Response(content_type='application/pdf',
            body=pdf_bytes(['Version 2 requires connect_safe. Never disable TLS.']))
    async with site(handler) as (base, options):
        payload = json.dumps({'data': [{'t': 0, 'rank': 1, 'url': base + '/paper.pdf',
            'title': 'Report', 'snippet': 'Provider snippet, not PDF evidence.'}]})
        cli = f"import sys\nprint('kagi 0.22.0' if '--version' in sys.argv else {payload!r})\n"
        async with running(tmp_path, acquisition_options=options, source=cli) as (url, _), client(url) as session:
            data = body(await session.call_tool('search', {'query': 'connect_safe'}))
            source = data['results'][0]
            assert source['document_type'] == 'pdf'
            assert source['provider_snippet'] == 'Provider snippet, not PDF evidence.'
            assert source['highlights'][0]['pages'] == [1]
            assert 'Never disable TLS.' in source['highlights'][0]['text']
            assert source['highlights'][0]['evidence_id'].startswith('p1.')


async def test_pdf_page_cap_and_no_text_pages_are_explicit_partial_evidence(tmp_path):
    async def handler(request):
        return web.Response(content_type='application/octet-stream',
            body=pdf_bytes(['Known public text.', '', 'Omitted page text.']))
    async with site(handler) as (base, options):
        options = replace(options, pdf_pages=2)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            data = body(await session.call_tool('fetch', {'urls': [base]}))
            source = data['sources'][0]
            assert data['budget']['partial_coverage'] is True
            assert source['pdf']['partial'] is True
            assert source['pdf']['omitted_pages'] == 1
            assert source['pdf']['unsupported_pages'] == [2]
            assert source['pdf']['references'][1]['status'] == 'unsupported_no_text'
            assert 'Omitted page text.' not in source['content']


@pytest.mark.parametrize('kind,code', [('empty', 'unsupported_format'),
    ('malformed', 'pdf_parse_failed'), ('encrypted', 'unsupported_format'), ('gzip', 'resource_limit'),
    ('timeout', 'fetch_timeout')])
async def test_pdf_failures_are_explicit_and_workers_reaped(tmp_path, kind, code):
    import gzip
    import psutil
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    async def handler(request):
        if kind == 'malformed':
            return web.Response(content_type='application/pdf', body=b'%PDF-1.7')
        if kind == 'encrypted':
            import io
            from pypdf import PdfWriter
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            writer.encrypt('fixture-password')
            output = io.BytesIO()
            writer.write(output)
            return web.Response(content_type='application/pdf', body=output.getvalue())
        if kind == 'gzip':
            return web.Response(content_type='application/pdf', body=gzip.compress(b'%PDF-' + b'x' * 100000),
                headers={'Content-Encoding': 'gzip'})
        return web.Response(content_type='application/pdf', body=pdf_bytes(['' if kind == 'empty' else 'Text']))
    async with site(handler) as (base, options):
        options = replace(options, response_bytes=4096,
            pdf_timeout=0.001 if kind == 'timeout' else 10)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
            assert source['error']['code'] == code
            assert not any(arg.endswith('pdf_worker.py') for p in psutil.Process().children(recursive=True) for arg in p.cmdline())
    assert {p.pid for p in psutil.Process().children(recursive=True)} <= before


async def test_pdf_object_stream_inflation_is_contained_by_parser_and_worker_bounds(tmp_path):
    import io
    import zlib
    from pypdf import PdfWriter
    from pypdf.generic import EncodedStreamObject, NameObject
    # Build a 700 MiB object expansion without allocating it in the fixture host.
    compressor = zlib.compressobj()
    compressed = b''.join(compressor.compress(b'x' * (1024 * 1024)) for _ in range(700)) + compressor.flush()
    stream = EncodedStreamObject()
    stream._data = compressed
    stream[NameObject('/Filter')] = NameObject('/FlateDecode')
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    async def handler(request):
        return web.Response(content_type='application/pdf', body=output.getvalue())
    async with site(handler) as (base, options):
        options = replace(options, pdf_memory_bytes=512 * 1024 * 1024)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=20) as session:
            source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
            assert source['error']['code'] == 'resource_limit'


async def test_pdf_forbidden_redirect_is_never_downloaded(tmp_path):
    hits = []
    async def private(request):
        hits.append(request.path)
        return web.Response(content_type='application/pdf', body=pdf_bytes(['Private']))
    async with site(private) as (sink, _):
        async def handler(request):
            raise web.HTTPFound(sink + '/private.pdf')
        async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            source = body(await session.call_tool('fetch', {'urls': [base + '/doc.pdf']}))['sources'][0]
            assert source['error']['code'] == 'policy_rejected'
            assert hits == []


async def test_pdf_parser_subprocess_never_inherits_service_credentials(tmp_path, monkeypatch, caplog):
    import sys
    executable = sys.executable
    marker = tmp_path / 'parser-isolated'
    wrapper = tmp_path / 'python-wrapper'
    wrapper.write_text(f'''#!{executable}
import os, sys
if '-I' in sys.argv and any(arg.endswith('pdf_worker.py') for arg in sys.argv):
    assert not set(os.environ) & {{'KAGI_SESSION_TOKEN', 'KAGI_CRAWLER_ACCESS_TOKEN', 'KAGI_API_KEY', 'UNRELATED_SECRET'}}
    assert os.environ['HOME'] == os.getcwd()
    import resource, runpy
    def observe(event, args):
        if event == 'import' and args[0] == 'pypdf':
            assert resource.getrlimit(resource.RLIMIT_AS) == (1536 * 1024 * 1024,) * 2
            assert resource.getrlimit(resource.RLIMIT_CPU) == (10, 10)
            assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
            import socket
            try:
                socket.socket()
            except PermissionError:
                pass
            else:
                raise AssertionError('parser must not create network sockets')
            open({str(marker)!r}, 'w').close()
    sys.addaudithook(observe)
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name='__main__')
    sys.exit(0)
os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])
''')
    wrapper.chmod(0o700)
    # Executable fixture at the OS subprocess boundary, like the CLI/driver
    # fixtures. No mocks of acquisition/cache/parser collaborators.
    monkeypatch.setattr(sys, 'executable', str(wrapper))
    for key in ('KAGI_SESSION_TOKEN', 'KAGI_CRAWLER_ACCESS_TOKEN', 'KAGI_API_KEY', 'UNRELATED_SECRET'):
        monkeypatch.setenv(key, 'fixture-secret-never-inherit')
    async def handler(request):
        assert 'fixture-secret-never-inherit' not in str(request.headers)
        return web.Response(content_type='application/pdf', body=pdf_bytes(['Isolated local parser.']))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [base]})
        assert 'Isolated local parser.' in body(result)['sources'][0]['content']
        assert marker.exists()
        assert 'fixture-secret-never-inherit' not in result.model_dump_json() + caplog.text


async def test_cancelled_pdf_worker_is_killed_and_mcp_recovers(tmp_path, monkeypatch):
    import sys
    import psutil
    from mcp import types
    executable = sys.executable
    marker = tmp_path / 'worker-started'
    wrapper = tmp_path / 'slow-python'
    wrapper.write_text(f'''#!{executable}
import os, sys, time
if '-I' in sys.argv and any(arg.endswith('pdf_worker.py') for arg in sys.argv) and not os.path.exists({str(marker)!r}):
    open({str(marker)!r}, 'w').write(str(os.getpid()))
    time.sleep(60)
os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])
''')
    wrapper.chmod(0o700)
    monkeypatch.setattr(sys, 'executable', str(wrapper))
    calls = []
    async def handler(request):
        return web.Response(content_type='application/pdf', body=pdf_bytes(['Recovered PDF text.']))
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, calls=calls) as session:
        task = asyncio.create_task(session.call_tool('fetch', {'urls': [base]}))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        await session.send_notification(types.ClientNotification(types.CancelledNotification(
            method='notifications/cancelled', params=types.CancelledNotificationParams(requestId=calls[-1]))))
        try:
            await task
        except Exception as error:
            assert 'cancel' in str(error).lower()
        async with asyncio.timeout(5):
            while psutil.pid_exists(pid):
                await asyncio.sleep(0.01)
        assert not psutil.pid_exists(pid)
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'Recovered PDF text.' in source['content']
