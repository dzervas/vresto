"""Bounded local PDF work after the common transport has acquired bytes."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

import anyio

from .process import Processes
from .transport import FetchError


async def parse_pdf(body, options):
    process = None
    reader = None
    # Only service-generated files; callers cannot select paths. Private HOME
    # prevents library initialization from reading user configuration/credentials.
    with tempfile.TemporaryDirectory(prefix='crawler-pdf-') as directory:
        path = Path(directory) / 'document.pdf'
        path.write_bytes(body)
        env = {key: os.environ[key] for key in ('PATH', 'LD_LIBRARY_PATH', 'LANG') if key in os.environ}
        env.update(HOME=directory, TMPDIR=directory, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
        try:
            async with asyncio.timeout(options.pdf_timeout):
                with anyio.CancelScope(shield=True):
                    process = await asyncio.create_subprocess_exec(sys.executable, '-I',
                        str(Path(__file__).with_name('pdf_worker.py')), str(path),
                        str(options.pdf_pages), str(options.pdf_memory_bytes),
                        str(max(1, int(options.pdf_timeout))), str(options.response_bytes),
                        env=env, cwd=directory, start_new_session=True,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL)
                async def read():
                    chunks = []
                    consumed = 0
                    while chunk := await process.stdout.read(8192):
                        consumed += len(chunk)
                        if consumed > options.response_bytes:
                            raise FetchError('resource_limit')
                        chunks.append(chunk)
                    return b''.join(chunks)
                reader = asyncio.create_task(read())
                raw = await reader
                await process.wait()
                if process.returncode:
                    raise FetchError('resource_limit' if process.returncode < 0 else 'pdf_parse_failed')
                result = json.loads(raw)
                if 'error' in result:
                    raise FetchError(result['error'])
        except TimeoutError:
            raise FetchError('fetch_timeout') from None
        except (ValueError, OSError):
            raise FetchError('pdf_parse_failed') from None
        finally:
            with anyio.CancelScope(shield=True):
                if reader is not None:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                if process is not None:
                    # Reader may have hit its cap; drain without storing to let
                    # asyncio reap a killed process with a full pipe buffer.
                    async def drain():
                        while await process.stdout.read(8192):
                            pass
                    discard = asyncio.create_task(drain())
                    await Processes.stop(process)
                    await discard
    content = ''
    references = []
    empty = []
    for number, raw_text in result['pages']:
        text = raw_text.replace('\r\n', '\n').replace('\r', '\n').strip()
        start = len(content)
        content += f'# PDF page {number}\n\n{text}\n\n'
        references.append({'page': number, 'start': start, 'end': len(content),
                           'status': 'text' if text else 'unsupported_no_text'})
        if not text:
            empty.append(number)
    if len(content.encode()) > options.response_bytes:
        raise FetchError('resource_limit')
    if not references or len(empty) == len(references):
        raise FetchError('unsupported_format')
    omitted = max(0, result['total_pages'] - len(references))
    return result['title'], content, {'total_pages': result['total_pages'],
        'processed_pages': len(references), 'omitted_pages': omitted,
        'unsupported_pages': empty, 'partial': bool(omitted or empty),
        'references': references, 'parser': 'crawl4ai_basic_text_no_ocr',
        'layout': 'not_reconstructed'}
