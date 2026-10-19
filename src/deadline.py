"""One inherited monotonic deadline across a tool's nested workflows."""
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_current: ContextVar[float | None] = ContextVar('request_deadline', default=None)


def remaining(default: float) -> float:
    deadline = _current.get()
    return default if deadline is None else max(0, deadline - asyncio.get_running_loop().time())


@contextmanager
def request_deadline(seconds: float, *, inherit: bool = True) -> Iterator[float]:
    # Nested fetches (including research citations) inherit the caller's budget.
    deadline = _current.get() if inherit else None
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + seconds
    token = _current.set(deadline)
    try:
        yield deadline
    finally:
        _current.reset(token)
