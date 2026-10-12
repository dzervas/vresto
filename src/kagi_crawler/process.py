"""Bounded CLI process ownership, including its process group (Linux deployment)."""
import asyncio
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
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*(self.stop(p) for p in tuple(self.active)))
