"""Local-only basic parser. Invoked by absolute service-owned path with -I.

Linux resource limits apply before importing pypdf. PDF object-stream
expansion has no aggregate hard byte counter: pypdf's native filter limits
and worker address-space/CPU caps contain it.
No OCR, image extraction, models, remote scraper, or layout fidelity claim.
"""
import contextlib
import json
import logging
import os
import resource
import sys


def main():
    path, pages, memory, cpu, output = sys.argv[1:]
    for kind, value in ((resource.RLIMIT_AS, int(memory)), (resource.RLIMIT_CPU, int(cpu)),
                        (resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, int(output))):
        resource.setrlimit(kind, (value, value))
    # Fail closed if the supported local parser ever acquires a network or exec
    # path. This is not a native-code sandbox; the deployment egress policy still
    # applies. Never inherit discovery/service credentials into this process.
    def audit(event, args):
        if event in ('socket.__new__', 'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                     'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.fork', 'os.forkpty'):
            raise PermissionError('local PDF parser only')
    sys.addaudithook(audit)
    logging.disable(logging.CRITICAL)
    resource_errors = (MemoryError,)
    try:
        from pypdf import PdfReader
        from pypdf.errors import LimitReachedError
        resource_errors += (LimitReachedError,)
        reader = PdfReader(path)
        # Encrypted documents are unsupported, not malformed text.
        if reader.is_encrypted:
            sys.stdout.buffer.write(b'{"error":"unsupported_format"}')
            return
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            # Page text is the concatenation of pypdf's text-visitor fragments
            # (no layout reconstruction), for at most the first `pages` pages.
            extracted = []
            for number, page in enumerate(reader.pages[:int(pages)], start=1):
                fragments = []
                page.extract_text(visitor_text=lambda text, *_: fragments.append(text))
                extracted.append([number, ''.join(fragments)])
            metadata = reader.metadata or {}
            response = {'title': metadata.get('/Title') or '', 'total_pages': len(reader.pages),
                        'pages': extracted}
        encoded = json.dumps(response, ensure_ascii=False).encode()
        if len(encoded) > int(output):
            encoded = b'{"error":"resource_limit"}'
    except resource_errors:
        encoded = b'{"error":"resource_limit"}'
    except Exception:
        encoded = b'{"error":"pdf_parse_failed"}'
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    main()
