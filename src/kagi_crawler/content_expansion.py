"""Bounded native read-only controls, not a general browser automation API.

Only details and structurally associated in-page text/discussion controls qualify.
Labels alone never authorize form submission, links, navigation or account actions.
Native page scripts/networking still require operator egress/resource containment.
"""
import asyncio

from patchright.async_api import Error

from .native_observation import NativeLoadObservation

_CONTROLS = 'details > summary, button, [role="button"], a'
_INSPECT = r'''elements => {
    const visible = el => el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    const scope = el => el.closest('main, article, [role="main"], .discussion-timeline');
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
        if (!visible(el) || el.closest('nav, header, footer, [role="dialog"]')) return;
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
        const candidate = more.test(label(el)) || collapsed;
        if (!candidate) return;
        // A heading-labelled discussion list plus a contained remaining-items
        // heading is a structural association; the button label alone is not.
        // Links masquerading as buttons and submit controls are not read-only.
        const safe = el.tagName === 'BUTTON' && !el.hasAttribute('form') &&
            el.type === 'button' && associated && scope(el) &&
            (more.test(label(el)) || (collapsed && textToggle.test(label(el))));
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
# Suppress only identical repeated *identified* discussion items. Different
# qualifications, authors, code or warnings under the same ID are never removed.
_DEDUP = '''() => {
    const seen = new Map();
    for (const el of document.querySelectorAll('[data-comment-id], .timeline-comment[id]')) {
        const id = el.getAttribute('data-comment-id') || el.id;
        const value = el.innerHTML;
        if (seen.get(id)?.has(value)) el.remove();
        else { if (!seen.has(id)) seen.set(id, new Set()); seen.get(id).add(value); }
    }
}'''


async def expand_content(page, options, *, deadline, revalidate):
    coverage = {'exhaustive': False, 'partial': False, 'actions': 0,
                'policy': 'bounded_read_only_in_page_controls', 'stop_reason': 'settled'}
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
                inspected = await controls.evaluate_all(_INSPECT)
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
                # The pre-action text is already an observation. An identical
                # first poll is not a DOM change: seeding with None added an
                # artificial 100ms interval to unchanged clicks/scrolls in the
                # local HTTP profile. Start the full quiet window after the
                # action; real text changes or pending native loads still reset
                # it, with the same revalidation and enclosing bounds. Matched
                # local-only raw timing/RSS, corpus/version/budget checks and
                # provenance: scripts/performance/hkq-{before,after}.json.
                previous = before
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
                await page.evaluate(_DEDUP)
                after = await page.evaluate('() => document.body.innerText')
                if before == after:
                    remaining = await controls.evaluate_all(_INSPECT)
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
    # Canonicalize exact identified repeats even in that partial document.
    if await page.evaluate(_SIZE, limits):
        await page.evaluate(_DEDUP)
    inspected = await controls.evaluate_all(_INSPECT)
    coverage['partial'] |= inspected['unsupported'] or inspected['action'] >= 0
    if inspected['unsupported'] and coverage['stop_reason'] in ('settled', 'no_progress'):
        coverage['stop_reason'] = 'unsupported_controls'
    return coverage
