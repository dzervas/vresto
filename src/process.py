"""Bounded CLI process ownership, including its process group (Linux deployment)."""
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import os
import signal

import anyio


from .discovery import DiscoveryError
from .deadline import remaining

if TYPE_CHECKING:
    from .server import Settings



class Processes:
    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.active = set()
        self.closing = False
        self.admitted = 0
        self.waiters: deque[asyncio.Future[bool]] = deque()

    # Bound CLI spawning separately from browsers; admission shares the request
    # deadline. Two 1 MiB output caps bound raw payloads, not subprocess RSS.
    CONCURRENCY = 2

    def release(self) -> None:
        self.admitted -= 1
        if self.waiters and not self.closing:
            waiter = self.waiters.popleft()
            self.admitted += 1
            waiter.set_result(True)

    @asynccontextmanager
    async def admission(self) -> AsyncIterator[None]:
        if self.closing:
            raise DiscoveryError('discovery_failed', reason='closing', phase='admission')
        if remaining(self.settings.timeout) <= 0:
            raise DiscoveryError('discovery_timeout', reason='service_deadline', phase='admission')
        waiter = None
        acquired = False
        try:
            if self.admitted < self.CONCURRENCY:
                self.admitted += 1
                acquired = True
            else:
                waiter = asyncio.get_running_loop().create_future()
                self.waiters.append(waiter)
                try:
                    with anyio.fail_after(remaining(self.settings.timeout)):
                        acquired = await asyncio.shield(waiter)
                except TimeoutError:
                    raise DiscoveryError('discovery_timeout', reason='service_deadline', phase='admission') from None
                if not acquired:
                    raise DiscoveryError('discovery_failed', reason='closing', phase='admission')
            yield
        finally:
            if waiter is not None:
                if waiter in self.waiters:
                    self.waiters.remove(waiter)
                elif waiter.done() and waiter.result():
                    # Admission may be granted at exactly the cancellation edge.
                    acquired = True
            if acquired:
                self.release()

    async def run(self, args: list[str], env: dict[str, str]) -> tuple[int, bytes, bytes]:
        if self.closing:
            raise DiscoveryError('discovery_failed', reason='closing', phase='spawn')
        if remaining(self.settings.timeout) <= 0:
            raise DiscoveryError('discovery_timeout', reason='service_deadline', phase='spawn')
        process = None
        readers = []
        phase = 'spawn'
        try:
            with anyio.fail_after(remaining(self.settings.timeout)):
                # Cancellation during spawn must not orphan a just-created child.
                with anyio.CancelScope(shield=True):
                    process = await asyncio.create_subprocess_exec(
                        *self.settings.command, *args, env=env, start_new_session=True,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    self.active.add(process)
                phase = 'execution'
                if self.closing:
                    raise DiscoveryError('discovery_failed', reason='closing', phase=phase)
                assert process is not None  # Shielded spawn completed.
                consumed = 0

                async def read(stream):
                    nonlocal consumed
                    chunks = []
                    while chunk := await stream.read(8192):
                        consumed += len(chunk)
                        if consumed > self.settings.max_output_bytes:
                            raise DiscoveryError('discovery_output_limit', reason='output_limit', phase='execution')
                        chunks.append(chunk)
                    return b"".join(chunks)

                readers = [asyncio.create_task(read(process.stdout)),
                           asyncio.create_task(read(process.stderr))]
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
                assert process.returncode is not None  # wait() reaped the child.
                return process.returncode, stdout, stderr
        except TimeoutError:
            raise DiscoveryError('discovery_timeout', reason='service_deadline', phase=phase,
                                 exception_type='TimeoutError') from None
        except OSError as error:
            raise DiscoveryError('discovery_failed', reason='os_error', phase=phase,
                                 exception_type=type(error).__name__) from None
        finally:
            # MCP cancellation and SDK shutdown use AnyIO cancel scopes. Shield
            # cleanup so TERM/KILL escalation and reaping complete under both.
            with anyio.CancelScope(shield=True):
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                if process is not None:
                    # A capped reader may leave a full asyncio pipe buffer. Drain
                    # without retaining bytes so transport EOF cannot deadlock wait().
                    async def discard(stream):
                        while await stream.read(8192):
                            pass
                    drains = [asyncio.create_task(discard(process.stdout)),
                              asyncio.create_task(discard(process.stderr))]
                    await self.stop(process)
                    await asyncio.gather(*drains)
                    self.active.discard(process)

    @staticmethod
    async def stop(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 0.5)
        except TimeoutError:
            pass
        # Kill remaining descendants too, even when the group leader exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def close(self) -> None:
        self.closing = True
        for waiter in self.waiters:
            waiter.set_result(False)
        self.waiters.clear()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*(self.stop(p) for p in tuple(self.active)))
