"""Service-owned tool correlation, never client IDs, queries or credentials."""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from uuid import uuid4


@dataclass
class _Request:
    identifier: str
    active: bool = True


_current: ContextVar[_Request | None] = ContextVar('diagnostic_request', default=None)


class _RequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        request = _current.get()
        record.__dict__['request_id'] = request.identifier if request is not None and request.active else '-'
        return True


logger = logging.getLogger('vresto')
logger.addFilter(_RequestFilter())


@contextmanager
def request_logging() -> Iterator[None]:
    request = _Request(uuid4().hex[:16])
    token = _current.set(request)
    try:
        yield
    finally:
        # Service-owned idle/ingestion tasks can inherit the context and outlive
        # this call. Their later logs must not point at a completed request.
        request.active = False
        _current.reset(token)
