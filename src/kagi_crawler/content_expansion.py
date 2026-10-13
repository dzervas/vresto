"""Bounded native read-only controls, not a general browser automation API.

Only details and explicitly associated in-page text/discussion controls qualify.
Labels alone never authorize form submission, links, navigation or account actions.
Native page scripts/networking still require operator egress/resource containment.
"""
import asyncio

from patchright.async_api import Error

_CONTROLS = 'details > summary, button, [role="button"], a'
_INSPECT = r'''elements => {
    const visible = el => el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    const scope = el => el.closest('main, article, [role="main"], .discussion-timeline');
    const label = el => (el.innerText || el.getAttribute('aria-label') || '').trim();
    const more = /^(load|show|view) (more|older)( (comments|replies|discussion|items))?[.!…]*$/i;
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
        const associated = targets.length && targets.every(target => target && scope(target));
        if (associated && el.getAttribute('aria-busy') === 'true') {
            unsupported = true;
            return;
        }
        const collapsed = el.getAttribute('aria-expanded') === 'false';
        const candidate = more.test(label(el)) || collapsed;
        if (!candidate) return;
        // Links masquerading as buttons and submit controls are not read-only.
        const safe = el.tagName === 'BUTTON' && !el.hasAttribute('form') &&
            el.type === 'button' && associated && scope(el) &&
            (more.test(label(el)) || (collapsed && textToggle.test(label(el))));
        if (safe && action < 0) action = index;
        if (!safe) unsupported = true;
    });
    return {action, unsupported};
}'''
_SIZE = '''limits => {
    let size = 0, nodes = 0;
    const walk = document.createTreeWalker(document, NodeFilter.SHOW_ALL);
    while (walk.nextNode()) {
        const node = walk.currentNode;
        if (++nodes > 50000) return false;
        size += 32 + (node.nodeValue || '').length * 6;
        for (const attr of node.attributes || []) size += (attr.name.length + attr.value.length) * 6 + 8;
        if (size > limits.dom) return false;
    }
    return (document.body?.innerText.length || 0) * 4 <= limits.content;
}'''
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


async def expand_content(page, options, *, deadline):
    coverage = {'exhaustive': False, 'partial': False, 'actions': 0,
                'policy': 'bounded_read_only_in_page_controls', 'stop_reason': 'settled'}
    limits = {'dom': options.dom_bytes, 'content': options.response_bytes}
    controls = page.locator(_CONTROLS)
    try:
        until = min(deadline, asyncio.get_running_loop().time() + options.expansion_timeout)
        async with asyncio.timeout_at(until):
            while True:
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
                await asyncio.sleep(options.expansion_wait)
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
    inspected = await controls.evaluate_all(_INSPECT)
    coverage['partial'] |= inspected['unsupported'] or inspected['action'] >= 0
    if inspected['unsupported'] and coverage['stop_reason'] in ('settled', 'no_progress'):
        coverage['stop_reason'] = 'unsupported_controls'
    return coverage
