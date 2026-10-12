"""All target sockets belong here, never to Chromium or route.fetch().

Acquisition is deliberately stateless: no caller headers, authentication or cookies
are forwarded. Cookie-dependent pages and POST APIs are not supported evidence.
"""
import asyncio
from dataclasses import dataclass, field
import ipaddress
from functools import partial
import socket
from urllib.parse import urlsplit, urlunsplit, urljoin
import zlib

import aiohttp
import anyio
from aiohttp.abc import AbstractResolver
from aiohttp.client_proto import ResponseHandler


class FetchError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AcquisitionOptions:
    # Initial containment defaults, not response budgets (.4) or measured parity.
    deadline: float = 30
    request_timeout: float = 10
    queue_wait: float = 10
    queue_capacity: int = 8
    response_bytes: int = 2 * 1024 * 1024
    job_bytes: int = 8 * 1024 * 1024
    max_requests: int = 64
    max_redirects: int = 5
    # PDF parsing is serialized under the existing acquisition permit, with
    # killable Linux worker containment independent of HTTP inflation bounds.
    pdf_pages: int = 100
    pdf_timeout: float = 10
    pdf_memory_bytes: int = 1536 * 1024 * 1024
    blocked_networks: tuple[str, ...] = ()
    # Only Python fixture harnesses can supply exact (host, port, IP) exceptions;
    # not MCP inputs, environment configuration or a production internal-URL flag.
    fixture_destinations: tuple[tuple[str, int, str], ...] = ()
    dns_lookup: object = field(default=None, repr=False)


class DestinationPolicy:
    def __init__(self, options):
        self.options = options
        self.networks = [ipaddress.ip_network(n) for n in options.blocked_networks]

    def address(self, host, port, value):
        if '%' in value:
            raise FetchError('policy_rejected')
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            raise FetchError('policy_rejected') from None
        if (host, port, str(ip)) in self.options.fixture_destinations:
            return
        if (not ip.is_global or str(ip) == '168.63.129.16'
            or any(ip in n for n in self.networks)
            or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo
                or ip in ipaddress.ip_network('64:ff9b::/96')
                or ip in ipaddress.ip_network('64:ff9b:1::/48')))):
            raise FetchError('policy_rejected')

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
            if port not in (80, 443) and not any(h == host and p == port for h, p, _ in self.options.fixture_destinations):
                raise ValueError()
            try:
                ipaddress.ip_address(host)
            except ValueError:
                # Reject historical integer/octal/hex IPv4 spellings: clients may
                # interpret these differently from DNS. Canonical literals only.
                try:
                    socket.inet_aton(host)
                except OSError:
                    pass
                else:
                    raise ValueError()
            else:
                self.address(host, port, host)
            authority = f'[{host}]' if ':' in host else host
            if parts.port is not None:
                authority += f':{port}'
            return urlunsplit((parts.scheme, authority, parts.path or '/', parts.query, ''))
        except (ValueError, UnicodeError):
            raise FetchError('policy_rejected') from None


class BoundedResponseHandler(ResponseHandler):
    """Bound aggregate headers BEFORE aiohttp's parser allocates header objects."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_header = bytearray()
        self.header_bytes = 0
        self.headers_done = False

    def data_received(self, data):
        if not self.headers_done:
            # data_received chunks themselves are bounded by asyncio's socket
            # reader; retain at most 32 KiB, including informational responses.
            remainder = data
            while not self.headers_done:
                room = 32768 - self.header_bytes - len(self.pending_header)
                candidate = bytes(self.pending_header) + remainder[:room + 1]
                end = candidate.find(b'\r\n\r\n')
                if end < 0:
                    if len(candidate) > 32768 - self.header_bytes:
                        self.set_exception(FetchError('resource_limit'))
                        self.close()
                        return
                    self.pending_header = bytearray(candidate)
                    break
                end += 4
                self.header_bytes += end
                if self.header_bytes > 32768:
                    self.set_exception(FetchError('resource_limit'))
                    self.close()
                    return
                status = candidate.split(b' ', 2)[1:2]
                self.headers_done = not (status and status[0].startswith(b'1'))
                consumed = end - len(self.pending_header)
                self.pending_header.clear()
                remainder = remainder[consumed:]
                if not remainder:
                    break
        super().data_received(data)


class PinnedConnector(aiohttp.TCPConnector):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Pinned aiohttp 3.13.3 has no aggregate response-header limit in the
        # public ClientSession API. This small protocol factory seam enforces it
        # before parsing, rather than counting already-allocated headers.
        self._factory = partial(BoundedResponseHandler, loop=self._loop)


class PinnedResolver(AbstractResolver):
    def __init__(self, policy):
        self.policy = policy

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        lookup = self.policy.options.dns_lookup
        if lookup is None:
            rows = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(row[4][0] for row in rows))
        else:
            addresses = await lookup(host, port)
        if not addresses:
            raise FetchError('fetch_failed')
        # Validate the WHOLE answer set, and hand these numerical addresses to
        # aiohttp's actual connector. No second unvalidated resolution occurs.
        for ip in addresses:
            self.policy.address(host, port, ip)
        return [dict(hostname=host, host=ip, port=port,
                     family=socket.AF_INET6 if ':' in ip else socket.AF_INET,
                     proto=0, flags=socket.AI_NUMERICHOST) for ip in addresses]

    async def close(self):
        pass


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes

    def fulfill_headers(self):
        # Hop-by-hop/framing and cookie state must not survive buffered fulfillment.
        excluded = {'content-encoding', 'content-length', 'transfer-encoding',
                    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
                    'te', 'trailer', 'upgrade', 'set-cookie'}
        excluded.update(v.strip().lower() for v in self.headers.get('connection', '').split(','))
        return {k: v for k, v in self.headers.items() if k not in excluded}


class Transport:
    def __init__(self, options):
        self.options = options
        self.policy = DestinationPolicy(options)
        self.requests = 0
        self.decoded = 0
        self.semaphore = asyncio.Semaphore(4)
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            connector=PinnedConnector(resolver=PinnedResolver(self.policy),
                use_dns_cache=False, force_close=True, limit=4),
            trust_env=False, auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=self.options.request_timeout),
            max_line_size=8192, max_field_size=8192,
            headers={'Accept-Encoding': 'gzip, identity', 'User-Agent': 'kagi-crawler/0.1'})
        # aiohttp 3.13.3 otherwise transparently retries disconnected GETs;
        # keep attempt accounting explicit and bounded (one attempt per hop).
        self.session._retry_connection = False
        return self

    async def __aexit__(self, *_):
        with anyio.CancelScope(shield=True):
            await self.session.close()

    async def fetch(self, url, *, redirects=True, method='GET', document=False):
        from .traversal import active_traversal
        traversal = active_traversal.get()
        for hop in range(self.options.max_redirects + 1):
            url = self.policy.url(url)
            if document and traversal is not None:
                traversal.check_document(url)
            async with self.semaphore:
                self.requests += 1
                if self.requests > self.options.max_requests:
                    raise FetchError('resource_limit')
                async with self.session.request(method, url, allow_redirects=False) as r:
                    headers = {k.lower(): v for k, v in r.headers.items()}
                    if 300 <= r.status < 400:
                        target = self.policy.url(urljoin(url, headers.get('location', '')))
                        if document and traversal is not None:
                            traversal.check_document(target)
                        if not redirects:
                            raise FetchError('rendering_unsupported')
                        if hop == self.options.max_redirects or 'location' not in headers:
                            raise FetchError('resource_limit')
                        url = target
                        continue
                    if r.status == 429:
                        raise FetchError('rate_limited')
                    if r.status in (401, 403):
                        raise FetchError('blocked')
                    if r.status >= 400:
                        raise FetchError('fetch_failed')
                    if r.content_length is not None and r.content_length > self.options.response_bytes:
                        raise FetchError('resource_limit')
                    encoding = headers.get('content-encoding', 'identity').lower()
                    if encoding not in ('identity', 'gzip'):
                        raise FetchError('unsupported_format')
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
                    wire = 0
                    body = bytearray()
                    async for chunk in r.content.iter_chunked(16384):
                        wire += len(chunk)
                        if wire > self.options.response_bytes:
                            raise FetchError('resource_limit')
                        remaining = min(self.options.response_bytes - len(body), self.options.job_bytes - self.decoded)
                        data = decoder.decompress(chunk, remaining + 1) if decoder else chunk
                        if len(data) > remaining or (decoder and decoder.unconsumed_tail):
                            raise FetchError('resource_limit')
                        if traversal is not None:
                            traversal.charge(len(data))
                        body.extend(data)
                        self.decoded += len(data)
                    if decoder and (not decoder.eof or decoder.unused_data):
                        raise FetchError('fetch_failed')
                    return Response(url, r.status, headers, bytes(body))
        raise FetchError('resource_limit')
