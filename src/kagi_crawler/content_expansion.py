"""Bounded native read-only controls, not a general browser automation API.

Only details and structurally associated in-page text/discussion controls qualify.
Labels alone never authorize form submission, links, navigation or account actions.
Native page scripts/networking still require operator egress/resource containment.
"""
from collections.abc import Awaitable, Callable
import asyncio

from patchright.async_api import Error

from patchright.async_api import Page
from .acquisition_policy import AcquisitionOptions
from .types import Coverage

from .native_observation import NativeLoadObservation

_CONTROLS = 'details > summary, button, [role="button"], a'
_SCOPE = 'main, article, [role="main"]'
_INSPECT = r'''(elements, {scopeSelector, authorizeSelector}) => {
    const visible = el => el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    const scope = el => el.closest(scopeSelector);
    const label = el => (el.innerText || el.getAttribute('aria-label') || '').trim();
    const more = /^(load|show|view) (more|older)( (comments|replies|discussion|items))?[.!…]*$/i;
    const references = (el, attr) => (el.getAttribute(attr) || '').split(/\s+/).filter(Boolean).map(id => document.getElementById(id));
    const discussionContinuation = el => {
        const list = el.closest('ol, ul, [role="list"]');
        const item = el.closest('li, [role="listitem"]');
        if (!list || !item || !list.contains(item) || !scope(list)) return false;
        const headings = references(list, 'aria-labelledby');
        const descriptions = references(el, 'aria-describedby');
        const entries = [...list.querySelectorAll(':scope > li, :scope > [role="listitem"]')];
        const attributed = entries.some(entry => entry !== item && (
            entry.querySelector('article [rel="author"]') ||
            (entry.querySelector('article h2, article h3, article h4') && entry.querySelector('article [datetime]')) ||
            [...entry.querySelectorAll('h2, h3, h4')].some(h =>
                /\S.+ (commented|replied|wrote|posted)\b/i.test(h.textContent) && h.querySelector('[datetime]'))));
        return headings.length && headings.every(h => h && /^H[1-6]$/.test(h.tagName) &&
            /^(activity|discussion|comments|replies)$/i.test(h.textContent.trim())) &&
            descriptions.length && descriptions.every(h => h && item.contains(h) && /^H[1-6]$/.test(h.tagName) &&
                /^\d+ remaining (items|comments|replies)$/i.test(h.textContent.trim())) &&
            entries.length > 1 && attributed;
    };
    const textToggle = /^(show|view|read) (more|details|text|code|spoiler)[.!…]*$/i;
    let unsupported = false;
    let action = -1;
    elements.forEach((el, index) => {
        if (!visible(el) || el.closest('nav, header, footer, dialog, [role="dialog"], [role="navigation"]')) return;
        if (el.closest('form')) {
            if (more.test(label(el))) unsupported = true;
            return;
        }
        if (el.tagName === 'SUMMARY') {
            if (!el.parentElement.open && action < 0) action = index;
            return;
        }
        const ids = (el.getAttribute('aria-controls') || '').split(/\s+/).filter(Boolean);
        const targets = ids.map(id => document.getElementById(id));
        const associated = (targets.length && targets.every(target => target && scope(target))) ||
            (more.test(label(el)) && discussionContinuation(el));
        if (associated && el.getAttribute('aria-busy') === 'true') {
            unsupported = true;
            return;
        }
        const collapsed = el.getAttribute('aria-expanded') === 'false';
        const authorized = !!authorizeSelector && el.matches(authorizeSelector);
        const candidate = more.test(label(el)) || collapsed || authorized;
        if (!candidate) return;
        // A heading-labelled discussion list plus a contained remaining-items
        // heading is a structural association; the button label alone is not.
        // Links masquerading as buttons and submit controls are not read-only.
        const href = (el.getAttribute('href') || '').trim();
        const control = (el.tagName === 'BUTTON' && el.type === 'button') ||
            (authorized && el.tagName === 'A' && (!href || href.startsWith('#')));
        const safe = control && !el.hasAttribute('form') && associated && scope(el) &&
            (authorized || more.test(label(el)) || (collapsed && textToggle.test(label(el))));
        if (safe && action < 0) action = index;
        if (!safe) unsupported = true;
    });
    return {action, unsupported};
}'''
# Shared with final serialization; expansion additionally bounds visible text.
DOM_WITHIN_LIMIT = '''limit => {
    let size = 0, nodes = 0;
    const walk = document.createTreeWalker(document, NodeFilter.SHOW_ALL);
    while (walk.nextNode()) {
        const node = walk.currentNode;
        if (++nodes > 50000) return false;
        size += 32 + (node.nodeValue || '').length * 6;
        for (const attr of node.attributes || []) size += (attr.name.length + attr.value.length) * 6 + 8;
        if (size > limit) return false;
    }
    return true;
}'''
_SIZE = ('limits => (' + DOM_WITHIN_LIMIT + ')(limits.dom) && '
         '(document.body?.innerText.length || 0) * 4 <= limits.content')


# data-comment-id explicitly declares item identity, independent of a site's
# CSS conventions. Remove only the exact same innerHTML under the same identity:
# different qualifications, authors, code and warnings always survive.
_DEDUP = '''() => {
    const seen = new Map();
    for (const el of document.querySelectorAll('[data-comment-id]')) {
        const id = el.getAttribute('data-comment-id');
        const value = el.innerHTML;
        if (seen.get(id)?.has(value)) el.remove();
        else { if (!seen.has(id)) seen.set(id, new Set()); seen.get(id).add(value); }
    }
}'''


async def expand_content(page: Page, options: AcquisitionOptions, *, deadline: float,
                         revalidate: Callable[[], Awaitable[bool]],
                         scope_selector: str = _SCOPE,
                         deduplicate: str = '() => {}',
                         authorize_selector: str | None = None,
                         on_failure: Callable[[Exception], None] | None = None) -> Coverage:
    coverage: Coverage = {'exhaustive': False, 'partial': False, 'actions': 0,
                'policy': 'bounded_read_only_in_page_controls', 'stop_reason': 'settled'}
    async def inspect_controls():
        nonlocal scope_selector, deduplicate, authorize_selector
        try:
            return await controls.evaluate_all(_INSPECT, {'scopeSelector': scope_selector,
                                                         'authorizeSelector': authorize_selector})
        except Exception as error:
            if on_failure is None or (scope_selector == _SCOPE and authorize_selector is None):
                raise
            on_failure(error)
            scope_selector, deduplicate, authorize_selector = _SCOPE, '() => {}', None
            return await controls.evaluate_all(_INSPECT, {'scopeSelector': scope_selector,
                                                         'authorizeSelector': authorize_selector})

    async def dedup():
        nonlocal scope_selector, deduplicate, authorize_selector
        await page.evaluate(_DEDUP)
        try:
            await page.evaluate(deduplicate)
        except Exception as error:
            if on_failure is None or deduplicate == '() => {}':
                raise
            on_failure(error)
            scope_selector, deduplicate, authorize_selector = _SCOPE, '() => {}', None

    limits = {'dom': options.dom_bytes, 'content': options.response_bytes}
    controls = page.locator(_CONTROLS)
    # Observe only native requests begun during expansion, not a pre-existing
    # analytics connection. Keep this window separate from initial rendering.
    native_loads = NativeLoadObservation(page)
    pending = native_loads.pending
    try:
        until = min(deadline, asyncio.get_running_loop().time() + options.expansion_timeout)
        async with asyncio.timeout_at(until):
            while True:
                if not await revalidate():
                    coverage.update(partial=True, stop_reason='document_replaced')
                    break
                if not await page.evaluate(_SIZE, limits):
                    coverage.update(partial=True, stop_reason='content_limit')
                    break
                inspected = await inspect_controls()
                if inspected['unsupported']:
                    coverage['partial'] = True
                if coverage['actions'] >= options.expansion_actions:
                    coverage.update(partial=True, stop_reason='action_limit')
                    break
                before = await page.evaluate('() => document.body.innerText')
                if inspected['action'] >= 0:
                    await controls.nth(inspected['action']).click(timeout=500)
                else:
                    # A bounded native scroll admits lazy-loaded public discussion;
                    # no inferred API pagination or separate downloads are used.
                    scrollable = await page.evaluate('() => document.documentElement.scrollHeight > window.innerHeight')
                    if not scrollable:
                        break
                    await page.evaluate('() => window.scrollTo(0, document.documentElement.scrollHeight)')
                coverage['actions'] += 1
                # A spinner or disappearing button is not loaded discussion.
                # Wait for post-action native fetch/XHR and a quiet visible DOM;
                # the enclosing duration/content/deadline caps still apply.
                # Seed with pre-action text so an unchanged first poll does not
                # restart the quiet window; text changes or pending loads do.
                previous = before
                # Performance follow-up: compare documentElement.scrollHeight before/after
                # the test scroll; if nothing grew, consider skipping the full 400 ms
                # quiet wait (live one-action no_progress expansions: ~0.77 s median).
                quiet_since = asyncio.get_running_loop().time()
                while True:
                    await asyncio.sleep(0.1)
                    if not await revalidate():
                        coverage.update(partial=True, stop_reason='document_replaced')
                        break
                    if not await page.evaluate(_SIZE, limits):
                        break
                    current = await page.evaluate('() => document.body.innerText')
                    now = asyncio.get_running_loop().time()
                    if current != previous or pending:
                        previous, quiet_since = current, now
                    if not pending and now - quiet_since >= options.expansion_wait:
                        break
                if coverage['stop_reason'] == 'document_replaced':
                    break
                if not await page.evaluate(_SIZE, limits):
                    coverage.update(partial=True, stop_reason='content_limit')
                    break
                await dedup()
                after = await page.evaluate('() => document.body.innerText')
                if before == after:
                    remaining = await inspect_controls()
                    coverage.update(partial=coverage['partial'] or remaining['action'] >= 0,
                                    stop_reason='no_progress')
                    break
    except TimeoutError:
        coverage.update(partial=True, stop_reason='duration_limit')
    except Error:
        # Detached/replaced controls do not turn an otherwise useful document
        # into a failure or trigger arbitrary retries. Cancellation propagates.
        coverage.update(partial=True, stop_reason='control_unavailable')
    finally:
        native_loads.close()
    # A duration bound may interrupt settling *after* content was appended.
    # An adapter can canonicalize exact identified repeats in that partial DOM.
    if await page.evaluate(_SIZE, limits):
        await dedup()
    inspected = await inspect_controls()
    coverage['partial'] |= inspected['unsupported'] or inspected['action'] >= 0
    if inspected['unsupported'] and coverage['stop_reason'] in ('settled', 'no_progress'):
        coverage['stop_reason'] = 'unsupported_controls'
    return coverage
