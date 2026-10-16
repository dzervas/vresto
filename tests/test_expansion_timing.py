"""Deterministic quiet-window semantics at the native browser/time boundaries."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from vresto import content_expansion
from vresto.acquisition_policy import AcquisitionOptions


class PhaseClock:
    def __init__(self, *, real_sleep=False):
        self.now = 0.0
        self.until = float('inf')
        self.real_sleep = real_sleep
        self.after_sleep = lambda: None

    def time(self):
        # A sub-nanosecond tie bias avoids float .7-.3 falling just below .4;
        # the behavioral intervals and (for HTTP) real sleeps stay unchanged.
        return self.now * (1 + 1e-9)

    @asynccontextmanager
    async def timeout_at(self, until):
        self.until = until
        yield

    async def sleep(self, delay):
        # Real HTTP tests retain every real native observation interval. Only
        # expansion's accounting clock is controlled: scheduler/roundtrip stalls
        # do not consume the virtual .49s regression budget. Never replace the
        # global event-loop clock or its deadlines, or shorten production waits.
        await asyncio.sleep(delay if self.real_sleep else 0)
        self.now = round(self.now + delay, 10)
        self.after_sleep()
        if self.now >= self.until:
            raise TimeoutError

    def install(self, monkeypatch):
        monkeypatch.setattr(content_expansion, 'asyncio', SimpleNamespace(
            get_running_loop=lambda: self, sleep=self.sleep, timeout_at=self.timeout_at))


class NativePage:
    """External browser boundary with controlled visible text/native events."""
    def __init__(self, clock, change):
        self.clock = clock
        self.change = change
        self.text = 'Warning: unchanged public text.'
        self.events = {}
        # Native requests are identity-hashed.
        self.request = type('Request', (), {'resource_type': 'fetch'})()
        self.clicked = False
        clock.after_sleep = self.advance

    def on(self, name, handler):
        self.events[name] = handler

    def remove_listener(self, name, handler):
        del self.events[name]

    def locator(self, selector):
        return self

    async def evaluate_all(self, script, argument=None):
        return {'action': 0, 'unsupported': False}

    def nth(self, index):
        return self

    async def click(self, timeout):
        self.clicked = True
        if self.change == 'pending':
            self.events['request'](self.request)

    def advance(self):
        if self.change == 'mutation' and self.clock.now >= .3:
            self.text = 'Warning: changed public qualification.'
        if self.change == 'pending' and self.clock.now == .3:
            self.events['requestfinished'](self.request)

    async def evaluate(self, script, argument=None):
        if script == '() => document.body.innerText':
            return self.text
        return True  # Resource checks and exact-repeat suppression succeed.


@pytest.mark.parametrize('change,settles_at', [('unchanged', .4), ('mutation', .7), ('pending', .6)])
async def test_expansion_retains_full_quiet_window_and_resets_for_native_changes(monkeypatch, change, settles_at):
    clock = PhaseClock()
    clock.install(monkeypatch)
    page = NativePage(clock, change)
    async def revalidate():
        return True
    coverage = await content_expansion.expand_content(page, AcquisitionOptions(expansion_actions=1),
                                                     deadline=5, revalidate=revalidate)
    assert clock.now == settles_at
    assert coverage['actions'] == 1
    assert not page.events  # Native observers cleaned up on every outcome.
