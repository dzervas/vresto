"""Metadata-only diagnostics at fake native/conversion boundaries; no Chromium."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import Error, TimeoutError as BrowserTimeout

from vresto import chromium as chromium_module
from vresto import browser as browser_module
from vresto.acquisition import Acquisition
from vresto.browser import Browser
from vresto.extraction import Extracted
from vresto.acquisition_policy import AcquisitionOptions, FetchError


URL = 'https://sentinel.invalid/private?token=sentinel-url-secret'
SECRET = 'sentinel-body-cookie-profile-credential'


class ConversionFailure(Exception):
    def __str__(self):
        raise AssertionError('diagnostics must not stringify exceptions')

    def __repr__(self):
        raise AssertionError('diagnostics must not repr exceptions')


class CleanupFailure(Exception):
    pass


class FakeBrowser:
    """Only the Acquisition contract; no native context, page or driver objects."""

    def __init__(self):
        self.connected = True
        self.start = AsyncMock()
        self.acquire = AsyncMock(return_value={
            'url': URL, 'title': 'Public title', 'content': 'Public text',
            'links': [], 'links_truncated': False, 'coverage': {}})
        self.close = AsyncMock(side_effect=self._close)

    def is_connected(self):
        return self.connected

    async def _close(self):
        self.connected = False


@pytest.fixture
def fake_browser():
    return FakeBrowser()


@pytest.fixture
def native(monkeypatch):
    response = SimpleNamespace(status=200, headers={'content-type': 'text/html'})
    page = SimpleNamespace(
        goto=AsyncMock(return_value=response),
        evaluate=AsyncMock(return_value=True),
        content=AsyncMock(return_value='<main>Public text</main>'),
        title=AsyncMock(return_value='Public title'), url=URL,
        on=lambda *args: None, close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    driver = SimpleNamespace(is_connected=lambda: driver.connected, connected=True)
    browser = Browser()
    browser.runtime.browser = driver
    browser.runtime.context = context
    browser.runtime.new_page = context.new_page
    browser.start = AsyncMock()
    browser._settled_response = AsyncMock(return_value=response)
    browser._settle_rendered = AsyncMock(return_value=(True, True, True, False))
    monkeypatch.setattr(browser_module, 'NativeLoadObservation',
                        lambda page: SimpleNamespace(pending=set(), close=lambda: None))
    monkeypatch.setattr(browser_module, 'expand_content', AsyncMock(return_value={
        'partial': False, 'stop_reason': 'settled'}))
    monkeypatch.setattr(browser_module, 'extract_document', lambda html, url, *, coverage=None: Extracted('Public text', [], False))
    return SimpleNamespace(browser=browser, driver=driver, context=context, page=page)


def safe_records(caplog):
    records = [r for r in caplog.records if r.name == 'vresto']
    assert records
    for record in records:
        assert record.exc_info is None and record.stack_info is None
        assert URL not in record.getMessage()
        assert SECRET not in record.getMessage()
        assert 'sentinel-url-secret' not in record.getMessage()
        assert all(isinstance(arg, (str, bool)) for arg in record.args)
    return [r.getMessage() for r in records]


def fail(error):
    def raising(*args, **kwargs):
        raise error
    return raising


@pytest.mark.parametrize('connected', [True, False])
async def test_adapter_failure_and_pre_retirement_state(fake_browser, caplog, connected):
    caplog.set_level(logging.INFO, logger='vresto')
    fake_browser.connected = connected
    fake_browser.acquire.side_effect = ConversionFailure(URL, SECRET)
    manager = Acquisition(browser_factory=lambda: fake_browser)
    assert await manager.warmup()
    close_observation = []

    async def close():
        close_observation.extend(safe_records(caplog))
        fake_browser.connected = False

    fake_browser.close.side_effect = close
    try:
        with pytest.raises(FetchError) as raised:
            await manager._acquire(URL, URL)
        assert raised.value.code == ('fetch_failed' if connected else 'browser_unavailable')
        if connected:
            fake_browser.close.assert_not_awaited()
            return
        messages = safe_records(caplog)
        assert f'browser_retire reason=unexpected_exception type=ConversionFailure connected={connected} stage=acquisition' in close_observation
        assert messages.count('browser_stop=completed') == 1
        fake_browser.close.assert_awaited_once()
    finally:
        await manager.close()


@pytest.mark.parametrize('error_type,connected,challenge,code,reason', [
    (Error, True, False, 'fetch_failed', 'native_error'),
    (Error, False, False, 'browser_unavailable', 'native_error'),
    (BrowserTimeout, True, False, 'fetch_timeout', 'native_timeout'),
    (BrowserTimeout, True, True, 'blocked', 'native_timeout'),
])
async def test_raw_native_type_before_public_wrapping(native, caplog, error_type, connected, challenge, code, reason):
    native.driver.connected = connected
    native.page.content.side_effect = error_type(URL + SECRET)
    # Direct boundary supplies challenge state without simulating challenge waits.
    state = {'stage': 'context', 'diagnosed': False, 'challenge': challenge,
             'http_status': 403, 'deadline': asyncio.get_running_loop().time() + 10}
    with pytest.raises(FetchError) as raised:
        await native.browser._acquire(URL, AcquisitionOptions(), state)
    assert raised.value.code == code
    if challenge:
        assert raised.value.details == {'reason': 'challenge_timeout', 'http_status': 403}
    messages = safe_records(caplog)
    assert messages == [f'acquisition_failure stage=dom_serialization type={error_type.__name__} connected={connected} reason={reason}']
    assert state['stage'] == 'dom_serialization'
    native.context.close.assert_not_awaited()
    assert native.page.close.await_count == 1


@pytest.mark.parametrize('primary', [False, True])
async def test_cleanup_failure_overrides_primary_as_before(native, monkeypatch, caplog, primary):
    if primary:
        monkeypatch.setattr(browser_module, 'extract_document', fail(ConversionFailure(URL, SECRET)))
    native.page.close.side_effect = CleanupFailure(URL + SECRET)
    with pytest.raises(CleanupFailure):
        await native.browser.acquire(URL, AcquisitionOptions())
    assert safe_records(caplog) == [
        'acquisition_failure stage=cleanup type=CleanupFailure connected=True reason=cleanup_failure']


async def test_successful_cleanup_restores_unexpected_failure_phase(native, monkeypatch, caplog):
    monkeypatch.setattr(browser_module, 'extract_document', fail(ConversionFailure(URL, SECRET)))
    with pytest.raises(ConversionFailure):
        await native.browser.acquire(URL, AcquisitionOptions())
    assert safe_records(caplog) == [
        'acquisition_failure stage=extraction type=ConversionFailure connected=True reason=unexpected_exception']


async def test_deadline_same_public_code_without_extra_native_log(native, caplog):
    native.page.content.side_effect = TimeoutError(URL + SECRET)
    with pytest.raises(FetchError) as raised:
        await native.browser.acquire(URL, AcquisitionOptions())
    assert raised.value.code == 'fetch_timeout'
    assert safe_records(caplog) == [
        'acquisition_failure stage=dom_serialization type=TimeoutError connected=True reason=deadline']


async def test_cancellation_still_propagates_and_closes_without_failure_log(native, caplog):
    native.page.content.side_effect = asyncio.CancelledError(URL + SECRET)
    with pytest.raises(asyncio.CancelledError):
        await native.browser.acquire(URL, AcquisitionOptions())
    native.context.close.assert_not_awaited()
    assert native.page.close.await_count == 1
    assert not [r for r in caplog.records if r.name == 'vresto']


async def test_success_has_no_failure_log(native, caplog):
    result = await native.browser.acquire(URL, AcquisitionOptions())
    assert result['content'] == 'Public text'
    native.context.close.assert_not_awaited()
    assert native.page.close.await_count == 1
    assert not [r for r in caplog.records if r.name == 'vresto']


async def test_startup_failure_logs_before_close_and_remains_unavailable(fake_browser, caplog):
    observation = []

    async def close():
        observation.extend(safe_records(caplog))
        fake_browser.connected = False

    fake_browser.start.side_effect = ConversionFailure(URL, SECRET)
    fake_browser.close.side_effect = close
    manager = Acquisition(browser_factory=lambda: fake_browser)
    assert await manager._warm() is False
    assert manager.browser is None
    assert observation == ['browser_retire reason=startup_failure type=ConversionFailure connected=True stage=startup']
    safe_records(caplog)


@pytest.mark.parametrize('stage', [
    'context', 'navigation', 'response_validation', 'render', 'expansion',
    'dom_serialization', 'extraction', 'metadata',
])
async def test_fixed_stage_boundaries(native, monkeypatch, caplog, stage):
    error = ConversionFailure(URL, SECRET)
    if stage == 'context':
        native.context.new_page.side_effect = error
    elif stage == 'navigation':
        native.page.goto.side_effect = error
    elif stage == 'response_validation':
        native.browser._settled_response.side_effect = error
    elif stage == 'render':
        native.browser._settle_rendered.side_effect = error
    elif stage == 'expansion':
        monkeypatch.setattr(browser_module, 'expand_content', AsyncMock(side_effect=error))
    elif stage == 'dom_serialization':
        native.page.content.side_effect = error
    elif stage == 'extraction':
        monkeypatch.setattr(browser_module, 'extract_document', fail(error))
    elif stage == 'metadata':
        native.page.title.side_effect = error
    with pytest.raises(ConversionFailure):
        await native.browser.acquire(URL, AcquisitionOptions())
    assert safe_records(caplog) == [
        f'acquisition_failure stage={stage} type=ConversionFailure connected=True reason=unexpected_exception']
    assert native.page.close.await_count == (0 if stage == 'context' else 1)


@pytest.mark.parametrize('error_type', [Error, BrowserTimeout])
async def test_native_tab_creation_keeps_original_unwrapped_exception(native, caplog, error_type):
    native.context.new_page.side_effect = error_type(URL + SECRET)
    with pytest.raises(error_type):
        await native.browser.acquire(URL, AcquisitionOptions())
    reason = 'native_timeout' if error_type is BrowserTimeout else 'native_error'
    assert safe_records(caplog) == [
        f'acquisition_failure stage=context type={error_type.__name__} connected=True reason={reason}']
    assert native.page.close.await_count == 0


async def test_controlled_retire_logs_connection_before_close(fake_browser, caplog):
    caplog.set_level(logging.INFO, logger='vresto')
    manager = Acquisition(browser_factory=lambda: fake_browser)
    manager.browser = fake_browser
    observation = []

    async def close():
        observation.extend(safe_records(caplog))
        fake_browser.connected = False

    fake_browser.close.side_effect = close
    await manager._retire()
    assert observation == ['browser_retire reason=controlled type=none connected=True stage=acquisition']
    assert safe_records(caplog)[-1] == 'browser_stop=completed'


@pytest.fixture(params=['handler', 'probe'])
def broken_diagnostics(request, caplog):
    class RaisingDiagnosticHandler(logging.Handler):
        def emit(self, record):
            if record.msg.startswith(('acquisition_failure ', 'browser_retire ')):
                raise RuntimeError('synthetic diagnostic handler failure')

    caplog.set_level(logging.INFO, logger='vresto')
    logger = logging.getLogger('vresto')
    handler = RaisingDiagnosticHandler()
    if request.param == 'handler':
        logger.addHandler(handler)

    def install_probe(driver):
        if request.param == 'probe':
            driver.is_connected = fail(RuntimeError('synthetic connection probe failure'))

    try:
        yield install_probe
    finally:
        logger.removeHandler(handler)


async def test_failed_diagnostics_do_not_skip_native_startup_cleanup(monkeypatch, broken_diagnostics):
    browser = Browser()
    driver = SimpleNamespace(is_connected=lambda: True, close=AsyncMock())
    broken_diagnostics(driver)
    original = ConversionFailure(URL, SECRET)

    class FailedDriverStart:
        async def __aenter__(self):
            browser.runtime.browser = driver
            raise original

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(chromium_module, '_DRIVER_START_LOCK', FailedDriverStart())
    with pytest.raises(ConversionFailure) as raised:
        await browser.start()
    assert raised.value is original
    driver.close.assert_awaited_once()
    assert browser.runtime.browser is None
    assert browser.runtime.profile is None


async def test_failed_diagnostics_do_not_skip_manager_startup_cleanup(fake_browser, broken_diagnostics):
    broken_diagnostics(fake_browser)
    fake_browser.start.side_effect = ConversionFailure(URL, SECRET)
    manager = Acquisition(browser_factory=lambda: fake_browser)
    assert await manager._warm() is False
    fake_browser.close.assert_awaited_once()
    assert manager.browser is None


@pytest.mark.parametrize('cleanup', [False, True])
async def test_failed_diagnostics_preserve_original_exception_and_page_cleanup(
        native, monkeypatch, broken_diagnostics, cleanup):
    broken_diagnostics(native.driver)
    original = CleanupFailure(URL, SECRET) if cleanup else ConversionFailure(URL, SECRET)
    if cleanup:
        native.page.close.side_effect = original
        # Cleanup must still replace a primary error, not a logging failure.
        monkeypatch.setattr(browser_module, 'extract_document', fail(ConversionFailure(URL, SECRET)))
    else:
        monkeypatch.setattr(browser_module, 'extract_document', fail(original))
    with pytest.raises(type(original)) as raised:
        await native.browser.acquire(URL, AcquisitionOptions())
    assert raised.value is original
    native.page.close.assert_awaited_once()


@pytest.mark.parametrize('error_type', [CleanupFailure, ConversionFailure])
async def test_failed_diagnostics_preserve_manager_mapping_and_retire_owned_browser(
        fake_browser, broken_diagnostics, error_type):
    broken_diagnostics(fake_browser)
    fake_browser.acquire.side_effect = error_type(URL, SECRET)
    fake_browser.connected = False
    manager = Acquisition(browser_factory=lambda: fake_browser)
    manager.browser = fake_browser
    manager.startup = asyncio.get_running_loop().create_future()
    manager.startup.set_result(True)
    with pytest.raises(FetchError) as raised:
        await manager._acquire(URL, URL)
    assert raised.value.code == 'browser_unavailable'
    fake_browser.close.assert_awaited_once()
    assert manager.browser is None
    assert not fake_browser.connected


async def test_failed_diagnostics_do_not_skip_controlled_retire(fake_browser, broken_diagnostics):
    broken_diagnostics(fake_browser)
    manager = Acquisition(browser_factory=lambda: fake_browser)
    manager.browser = fake_browser
    await manager._retire()
    fake_browser.close.assert_awaited_once()
    assert manager.browser is None
    assert not fake_browser.connected


@pytest.mark.parametrize('helper', ['browser', 'manager'])
async def test_diagnostic_helpers_do_not_swallow_baseexception(native, fake_browser, helper):
    original = asyncio.CancelledError()
    native.driver.is_connected = fail(original)
    fake_browser.is_connected = fail(original)
    with pytest.raises(asyncio.CancelledError) as raised:
        if helper == 'browser':
            native.browser._diagnostic(ConversionFailure(), 'extraction', 'unexpected_exception')
        else:
            Acquisition._lifecycle_diagnostic(fake_browser, 'unexpected_exception', ConversionFailure(), 'acquisition')
    assert raised.value is original


async def test_shared_warmup_and_success_reuse_adapter(fake_browser):
    started, release = asyncio.Event(), asyncio.Event()

    async def start():
        started.set()
        await release.wait()

    fake_browser.start.side_effect = start
    manager = Acquisition(browser_factory=lambda: fake_browser)
    try:
        first = manager.warmup()
        await started.wait()
        assert manager.warmup() is first
        release.set()
        assert await first
        for _ in range(2):
            result = await manager.fetch_many([URL])
            assert result['sources'][0]['content'] == 'Public text'
        fake_browser.start.assert_awaited_once()
        assert fake_browser.acquire.await_count == 2
        assert manager.waiting == 0 and manager.active == 0
    finally:
        await manager.close()
    fake_browser.close.assert_awaited_once()
    assert manager.warmup() is None


async def test_queue_capacity_and_wait_with_fake_adapter(fake_browser):
    entered, release = asyncio.Event(), asyncio.Event()
    document = fake_browser.acquire.return_value

    async def acquire(url, options):
        entered.set()
        await release.wait()
        return document

    fake_browser.acquire.side_effect = acquire
    manager = Acquisition(AcquisitionOptions(browser_concurrency=1, queue_capacity=1, queue_wait=0.05),
                          browser_factory=lambda: fake_browser)
    active = asyncio.create_task(manager.fetch_many([URL]))
    queued = None
    try:
        await entered.wait()
        queued = asyncio.create_task(manager.fetch_many([URL]))
        async with asyncio.timeout(1):
            while manager.waiting != 1:
                await asyncio.sleep(0)
        excess = await manager.fetch_many([URL])
        assert excess['sources'][0]['error']['code'] == 'queue_full'
        assert (await queued)['sources'][0]['error']['code'] == 'queue_timeout'
        assert manager.waiting == 0
        release.set()
        assert (await active)['sources'][0]['content'] == 'Public text'
        assert manager.active == 0
        fake_browser.acquire.assert_awaited_once()
    finally:
        release.set()
        await manager.close()
        await asyncio.gather(active, *([queued] if queued else []), return_exceptions=True)


@pytest.mark.parametrize('during_startup', [False, True])
async def test_cancellation_preserves_shared_adapter_and_releases_permit(fake_browser, during_startup):
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    boundary = fake_browser.start if during_startup else fake_browser.acquire
    boundary.side_effect = blocked
    manager = Acquisition(browser_factory=lambda: fake_browser)
    active = asyncio.create_task(manager.fetch_many([URL]))
    try:
        await entered.wait()
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert (manager.browser is None) == during_startup
        assert manager.waiting == 0 and manager.active == 0
        fake_browser.close.assert_not_awaited()
    finally:
        await manager.close()


async def test_idle_retirement_waits_for_active_fake_acquisition(fake_browser):
    now = [0.0]
    sleeping, wake = asyncio.Event(), asyncio.Event()
    entered, release = asyncio.Event(), asyncio.Event()
    document = fake_browser.acquire.return_value

    async def sleep(delay):
        sleeping.set()
        await wake.wait()
        wake.clear()

    async def acquire(url, options):
        entered.set()
        await release.wait()
        return document

    fake_browser.acquire.side_effect = acquire
    manager = Acquisition(clock=lambda: now[0], sleep=sleep,
                          browser_factory=lambda: fake_browser)
    active = asyncio.create_task(manager.fetch_many([URL]))
    try:
        await entered.wait()
        await sleeping.wait()
        now[0] = 3600
        wake.set()
        await asyncio.sleep(0)
        fake_browser.close.assert_not_awaited()
        release.set()
        assert (await active)['sources'][0]['content'] == 'Public text'
        assert manager.last_work == 3600
        fake_browser.close.assert_not_awaited()
        now[0] = 7200
        wake.set()
        await manager.idle_task
        fake_browser.close.assert_awaited_once()
        assert manager.browser is None
    finally:
        release.set()
        await manager.close()
        await asyncio.gather(active, return_exceptions=True)
