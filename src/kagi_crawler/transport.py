"""Acquisition options and input screening, NOT destination/network isolation.

User-approved departure from the original interception spec: Chromium owns all
networking. Literal URL checks neither resolve DNS nor constrain native redirects,
subresources, workers or sockets. Operators must enforce egress externally.
"""
from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class FetchError(Exception):
    def __init__(self, code, *, reason=None, http_status=None):
        self.code = code
        # Only fixed classifications and the main HTTP status, never raw
        # headers, challenge tokens, page bodies or exception strings.
        self.details = {}
        if reason is not None:
            self.details['reason'] = reason
        if http_status is not None:
            self.details['http_status'] = http_status
        super().__init__(code)


@dataclass(frozen=True)
class AcquisitionOptions:
    deadline: float = 30
    request_timeout: float = 10
    queue_wait: float = 10
    queue_capacity: int = 8
    # Conservative rendered-DOM estimate and serialized HTML input limit.
    # Separate from normalized Markdown/downloaded PDF parser input below.
    dom_bytes: int = 10 * 1024 * 1024
    # Post-acquisition text/PDF limit, NOT streamed native resource containment.
    response_bytes: int = 2 * 1024 * 1024
    # Within the existing native context and request deadline; not network caps.
    expansion_timeout: float = 2
    expansion_actions: int = 8
    # Deterministic native scroll fixture delivers text after 300 ms. Allow
    # 400 ms per action, still inside the independent 2 s expansion cap.
    expansion_wait: float = 0.4
    pdf_pages: int = 100
    pdf_timeout: float = 10
    pdf_memory_bytes: int = 1536 * 1024 * 1024
    fixture_destinations: tuple[tuple[str, int, str], ...] = ()

    def __post_init__(self):
        if not 0.01 <= self.expansion_timeout <= 10:
            raise ValueError('expansion duration must be 0.01–10 seconds')
        if type(self.expansion_actions) is not int or not 0 <= self.expansion_actions <= 32:
            raise ValueError('expansion actions must be 0–32')
        if not 0.01 <= self.expansion_wait <= 1:
            raise ValueError('expansion settle wait must be 0.01–1 second')
        if not 0.01 <= self.deadline <= 120:
            raise ValueError('acquisition deadline must be 0.01–120 seconds')
        if not 0.01 <= self.queue_wait <= 30:
            raise ValueError('queue wait must be 0.01–30 seconds')
        if type(self.queue_capacity) is not int or not 1 <= self.queue_capacity <= 32:
            raise ValueError('queue capacity must be 1–32')


class DestinationPolicy:
    def __init__(self, options):
        self.options = options

    def url(self, value):
        try:
            if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
                raise ValueError()
            parts = urlsplit(value)
            if parts.scheme not in ('http', 'https') or parts.username is not None or parts.password is not None:
                raise ValueError()
            host = parts.hostname
            if not host or '%' in host:
                raise ValueError()
            host = host.rstrip('.').encode('idna').decode('ascii').lower()
            port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
            fixture = any(h == host and p == port for h, p, _ in self.options.fixture_destinations)
            if not fixture and (port not in (80, 443) or host == 'localhost' or host.endswith('.localhost')):
                raise ValueError()
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                try:
                    socket.inet_aton(host)
                except OSError:
                    pass
                else:
                    raise ValueError()
            else:
                if not fixture and (not ip.is_global or str(ip) == '168.63.129.16'
                    or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo
                        or ip in ipaddress.ip_network('64:ff9b::/96')
                        or ip in ipaddress.ip_network('64:ff9b:1::/48')))):
                    raise ValueError()
            authority = f'[{host}]' if ':' in host else host
            if parts.port is not None:
                authority += f':{port}'
            return urlunsplit((parts.scheme, authority, parts.path or '/', parts.query, ''))
        except (ValueError, UnicodeError):
            raise FetchError('policy_rejected') from None
