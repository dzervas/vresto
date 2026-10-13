"""Bounded CLI process ownership, including its process group (Linux deployment)."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import os
import signal

import anyio


class DiscoveryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Processes:
    def __init__(self, settings):
        self.settings = settings
        self.active = set()
        self.closing = False
        self.admitted = 0
        self.waiters = deque()

    # Separate from browser acquisition: at most two discovery jobs (each owns
    # version + search sequentially), eight waiting, ten seconds admission wait.
    # The request's 30s deadline includes admission. Provisional containment, not
    # provider parity; at most 2 MiB raw CLI payload, excluding join/parse/pipe
    # overhead and subprocess RSS. Real TCP MCP/executable fixture observed two
    # running searches + eight non-spawning waiters; eleventh rejected, all ten
    # completed after release. Ten-second wait and shared shorter deadlines are
    # exercised separately. Deployment CLI CPU/RSS/latency remain unmeasured.
    CONCURRENCY = 2
    QUEUE_CAPACITY = 8
    QUEUE_WAIT = 10

    def release(self):
        self.admitted -= 1
        if self.waiters and not self.closing:
            waiter = self.waiters.popleft()
            self.admitted += 1
            waiter.set_result(True)

    @asynccontextmanager
    async def admission(self):
        if self.closing:
            raise DiscoveryError('discovery_failed')
        waiter = None
        acquired = False
        try:
            if self.admitted < self.CONCURRENCY:
                self.admitted += 1
                acquired = True
            else:
                if len(self.waiters) >= self.QUEUE_CAPACITY:
                    raise DiscoveryError('discovery_queue_full')
                waiter = asyncio.get_running_loop().create_future()
                self.waiters.append(waiter)
                try:
                    with anyio.fail_after(self.QUEUE_WAIT):
                        acquired = await asyncio.shield(waiter)
                except TimeoutError:
                    raise DiscoveryError('discovery_queue_timeout') from None
                if not acquired:
                    raise DiscoveryError('discovery_failed')
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

    async def run(self, args, env):
        if self.closing:
            raise DiscoveryError("discovery_failed")
        process = None
        readers = []
        try:
            with anyio.fail_after(self.settings.timeout):
                # Cancellation during spawn must not orphan a just-created child.
                with anyio.CancelScope(shield=True):
                    process = await asyncio.create_subprocess_exec(
                        *self.settings.command, *args, env=env, start_new_session=True,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    self.active.add(process)
                if self.closing:
                    raise DiscoveryError('discovery_failed')
                consumed = 0

                async def read(stream):
                    nonlocal consumed
                    chunks = []
                    while chunk := await stream.read(8192):
                        consumed += len(chunk)
                        if consumed > self.settings.max_output_bytes:
                            raise DiscoveryError("discovery_output_limit")
                        chunks.append(chunk)
                    return b"".join(chunks)

                readers = [asyncio.create_task(read(process.stdout)),
                           asyncio.create_task(read(process.stderr))]
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
                return process.returncode, stdout, stderr
        except TimeoutError:
            raise DiscoveryError("discovery_timeout") from None
        except OSError:
            raise DiscoveryError("discovery_failed") from None
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
    async def stop(process):
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

    async def close(self):
        self.closing = True
        for waiter in self.waiters:
            waiter.set_result(False)
        self.waiters.clear()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*(self.stop(p) for p in tuple(self.active)))
