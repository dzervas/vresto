"""Read-only public expansion observed through the real HTTP MCP boundary."""
import json
import os
import pytest
from aiohttp import web
from test_fetch import site
from test_http import running, client, body, cli_response



async def test_fetch_expands_collapsed_text_and_lazy_discussion_then_search_reuses_it(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<title>Cancellation discussion</title>
        <main><h1>TaskGroup cancellation</h1><details ontoggle="if(this.open && !this.dataset.loaded) { this.dataset.loaded='1'; this.insertAdjacentHTML('beforeend', '<p>Native expanded cancellation caveat.</p>'); }"><summary>Technical caveat</summary>
        <p>Warning: uncancel does not restore cancelled work.</p><pre><code>TaskGroup._abort()</code></pre></details>
        <section id="comments"><article data-comment-id="one"><h2>Alice</h2><p>Initial discussion.</p></article></section>
        <button type="button" aria-controls="comments" onclick="load()">Load more comments</button>
        <script>function load() { document.querySelector('#comments').insertAdjacentHTML('beforeend',
        '<article data-comment-id="one"><h2>Alice</h2><p>Initial discussion.</p></article>' +
        '<article data-comment-id="two"><h2>Bob</h2><p>cancel_called is not reliable before version 3.13.</p></article>');
        document.querySelector('button').remove(); }</script></main>''')
    async with site(handler) as (base, options):
        source = cli_response(json.dumps({'data': [{'t': 0, 'rank': 1, 'url': base, 'title': 'Discussion'}]}))
        async with running(tmp_path, acquisition_options=options, source=source) as (url, _), client(url) as session:
            fetched = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert 'error' not in fetched, fetched
            for text in ['Warning: uncancel', 'TaskGroup._abort()', 'Native expanded cancellation caveat.', 'Bob', 'cancel_called is not reliable before version 3.13.']:
                assert text in fetched['content']
            assert fetched['content'].count('Initial discussion.') == 1
            assert fetched['content'].index('Alice') < fetched['content'].index('Bob')
            assert fetched['coverage']['exhaustive'] is False
            searched = body(await session.call_tool('search', {'query': 'cancel_called', 'num_results': 1}))['results'][0]
            assert searched['content_version'] == fetched['content_version']
            assert any('cancel_called' in h['text'] for h in searched['highlights'])


async def test_duration_bound_still_suppresses_loaded_exact_repeats(tmp_path):
    from dataclasses import replace
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Repeated discussion</h1>
        <section id="comments"><article data-comment-id="one"><p>Warning: repeated public qualification.</p></article></section>
        <button type="button" aria-controls="comments" onclick="document.querySelector('#comments').insertAdjacentHTML('beforeend', document.querySelector('article').outerHTML)">Load more comments</button></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(options,
            expansion_timeout=0.2)) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert source['coverage']['stop_reason'] == 'duration_limit'
        assert source['content'].count('Warning: repeated public qualification.') == 1

async def test_unsupported_account_controls_are_not_clicked_and_coverage_is_disclosed(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Public discussion</h1>
        <section id="comments"><p>Warning: preserve the original qualification.</p></section>
        <button type="button" aria-controls="comments" aria-expanded="false"
          onclick="document.querySelector('#comments').textContent='Account action executed'">Delete comments</button>
        <form><button>Load more comments</button></form>
        <a role="button" href="/account">Load more comments</a></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base]}))
        source = data['sources'][0]
        assert 'Warning: preserve the original qualification.' in source['content']
        assert 'Account action executed' not in source['content']
        assert source['coverage']['partial']
        assert source['coverage']['stop_reason'] == 'unsupported_controls'
        assert data['budget']['partial_coverage']


async def test_action_bound_is_public_and_old_expanded_evidence_survives_refresh(tmp_path):
    from dataclasses import replace
    version = '3.12'
    async def handler(request):
        return web.Response(content_type='text/html', text=f'''<main><h1>Cancellation</h1>
        <section id="comments"><article data-comment-id="one"><h2>Alice</h2>
        <p>cancel_called is unsafe in version {version}. Do not ignore this warning.</p></article></section>
        <button type="button" aria-controls="comments" onclick="document.querySelector('#comments').insertAdjacentHTML('beforeend',
        '<article data-comment-id=one><p>Additional differing qualification in version {version}.</p></article>')">Load more comments</button></main>''')
    async with site(handler) as (base, options):
        options = replace(options, expansion_actions=1)
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
            data = body(await session.call_tool('fetch', {'urls': [base], 'query': 'cancel_called'}))
            original = data['sources'][0]
            assert original['coverage']['stop_reason'] == 'action_limit'
            assert original['coverage']['actions'] == 1
            assert data['budget']['partial_coverage']
            assert data['acquisition_limits']['expansion_actions'] == 1
            assert data['acquisition_limits']['expansion_seconds'] == 10
            quote = original['highlights'][0]
            version = '3.14'
            current = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert 'Additional differing qualification in version 3.14.' in current['content']
            assert current['content_version'] != original['content_version']
            expanded = body(await session.call_tool('expand', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert expanded['highlights'] == [quote]
            assert expanded['content_version'] == original['content_version']
            assert '3.14' not in str(expanded)


async def test_scroll_retrieves_delayed_public_discussion(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main style="min-height:2400px"><h1>Lazy discussion</h1>
        <section id="comments"><p>Initial public discussion.</p></section></main>
        <script>let loaded=false; window.addEventListener('scroll', () => {
        if (loaded) return; loaded=true;
        setTimeout(() => document.querySelector('#comments').insertAdjacentHTML('beforeend',
        '<article><h2>Carol</h2><p>Warning: lazy_cancel is unsupported before version 3.13.</p></article>'), 300);
        });</script>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
        assert 'Carol' in source['content']
        assert 'Warning: lazy_cancel is unsupported before version 3.13.' in source['content']
        assert source['coverage']['exhaustive'] is False


async def test_unchanged_expansion_uses_known_text_without_an_extra_quiet_interval(tmp_path, monkeypatch):
    from dataclasses import replace
    from test_expansion_timing import PhaseClock
    clock = PhaseClock(real_sleep=True)
    clock.install(monkeypatch)
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Stable discussion</h1>
        <section id="comments"><p>Warning: no available public continuation in Version 2.</p></section>
        <button type="button" aria-controls="comments">Load more comments</button></main>''')
    # Public coverage with test-only expansion phase accounting, not an
    # aggressive wall-time threshold. Real HTTP/browser calls and real 100ms
    # sleeps remain; native roundtrips/scheduler stalls do not consume the
    # virtual .49s bound. An artificial first-poll reset needs five intervals
    # and deterministically exhausts it; the full real 400ms quiet window fits.
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(
            options, expansion_timeout=.49)) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))
        source = data['sources'][0]
        assert source['coverage']['stop_reason'] == 'no_progress'
        assert source['coverage']['actions'] == 1
        assert clock.now == .4  # Four real 100ms observations, not early capture.
        assert source['coverage']['partial'] and data['budget']['partial_coverage']
        assert 'Warning: no available public continuation in Version 2.' in source['content']


async def test_duration_and_no_progress_bounds_keep_initial_document(tmp_path):
    from dataclasses import replace
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Bounded discussion</h1>
        <section id="comments"><p>Warning: no available public continuation.</p></section>
        <button type="button" aria-controls="comments">Load more comments</button></main>''')
    async with site(handler) as (base, options):
        for path, limit, reason in [('/duration', 0.1, 'duration_limit'), ('/stalled', 2, 'no_progress')]:
            async with running(tmp_path, acquisition_options=replace(options, expansion_timeout=limit)) as (url, _), client(url) as session:
                data = body(await session.call_tool('fetch', {'urls': [base + path], 'freshness': 'fresh_only'}))
                source = data['sources'][0]
                assert 'Warning: no available public continuation.' in source['content']
                assert source['coverage']['stop_reason'] == reason
                assert source['coverage']['partial']
                assert data['budget']['partial_coverage']


# Opt-in only: choose an authorized public page and a curated literal known to
# occur in its expanded discussion (newline-separated literals are supported).
# No fixture/private-destination exception,
# credentials, Python downloads or caller JS are supplied to acquisition.
# VRESTO_EXPANSION_SMOKE_URL=https://... \
# VRESTO_EXPANSION_SMOKE_EXPECTED='qualified loaded text' pytest -q ...
@pytest.mark.skipif(not os.environ.get('VRESTO_EXPANSION_SMOKE_URL'), reason='authorized public smoke is opt-in')
async def test_opt_in_public_expansion_fetch_and_composed_search(tmp_path):
    target = os.environ['VRESTO_EXPANSION_SMOKE_URL']
    expected = os.environ['VRESTO_EXPANSION_SMOKE_EXPECTED'].splitlines()
    source = cli_response(json.dumps({'data': [{'t': 0, 'rank': 1, 'url': target, 'title': 'Public discussion'}]}))
    async with running(tmp_path, source=source, response_budget_bytes=262144) as (url, _), client(url, timeout=60) as session:
        fetched = body(await session.call_tool('fetch', {'urls': [target], 'freshness': 'fresh_only'}))['sources'][0]
        assert 'error' not in fetched
        assert all(text in fetched['content'] for text in expected), fetched
        searched = body(await session.call_tool('search', {'query': expected[0], 'num_results': 1, 'freshness': 'fresh_only'}))['results'][0]
        assert expected[0] in ' '.join(h['text'] for h in searched['highlights']), searched
        # Independently inspect the acquired canonical search document; a
        # response/evidence budget must not masquerade as missing acquisition.
        canonical = body(await session.call_tool('fetch', {'urls': [target], 'freshness': 'ordinary'}))['sources'][0]
        assert 'content' not in fetched.get('truncated_fields', [])
        assert 'content' not in canonical.get('truncated_fields', [])
        assert canonical['content_version'] == searched['content_version']
        assert all(text in canonical['content'] for text in expected), canonical
        assert fetched['coverage']['exhaustive'] is False
        print('public HTTP fresh fetch and fresh composed search passed; canonical literals:', expected,
              'fetch coverage:', fetched['coverage'], 'search coverage:', searched['coverage'])


async def test_expansion_document_growth_obeys_resource_limit(tmp_path):
    from dataclasses import replace
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Bounded growth</h1>
        <section id="comments"><p>Initial discussion.</p></section>
        <button type="button" aria-controls="comments" onclick="document.querySelector('#comments').append('x'.repeat(20000))">Load more comments</button></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(options,
            dom_bytes=4096, response_bytes=4096)) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert source['error']['code'] == 'resource_limit'
        assert 'content' not in source


async def test_unavailable_busy_continuation_is_partial_not_exhaustive(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Unavailable continuation</h1>
        <section id="comments"><p>Useful initial public evidence.</p></section>
        <button type="button" aria-controls="comments" onclick="this.textContent='Loading...';this.setAttribute('aria-busy','true')">Load more comments</button></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base]}))
        assert 'Useful initial public evidence.' in data['sources'][0]['content']
        assert data['sources'][0]['coverage']['partial']
        assert data['budget']['partial_coverage']


async def test_expansion_does_not_spend_the_remaining_acquisition_deadline(tmp_path):
    from dataclasses import replace
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Useful bounded document</h1>
        <section id="comments"><p>Warning: continuation remains unavailable.</p></section>
        <button type="button" aria-controls="comments"
          onclick="setInterval(() => document.querySelector('#comments').append(' Still loading.'), 100)">Load more comments</button></main>''')
    # Leave room for idle plus quiet-DOM observation, but less than the 10s expansion bound.
    # Post-click timer mutations force the original deadline, not no-progress, to stop expansion.
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(options,
            deadline=5)) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'error' not in source, source
        assert 'Warning: continuation remains unavailable.' in source['content']
        assert source['coverage']['partial']
        assert source['coverage']['stop_reason'] == 'duration_limit'


@pytest.mark.parametrize('control', ['<form><button>Load more comments</button></form>',
    '<a href="/next">Load more comments</a>'])
async def test_unsupported_form_and_link_pagination_are_disclosed_without_visiting(tmp_path, control):
    visits = []
    async def handler(request):
        visits.append(request.path)
        return web.Response(content_type='text/html', text=f'<main><h1>Public discussion</h1><p>Initial evidence.</p>{control}</main>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        data = body(await session.call_tool('fetch', {'urls': [base]}))
        assert visits == ['/']
        assert data['sources'][0]['coverage']['partial']
        assert data['sources'][0]['coverage']['stop_reason'] == 'unsupported_controls'



@pytest.mark.parametrize('replacement, expected', [('challenge', 'fetch_timeout'), ('error', 'fetch_failed')])
async def test_expansion_replacement_is_revalidated_and_never_cached_as_evidence(tmp_path, replacement, expected):
    from dataclasses import replace
    async def handler(request):
        if request.path == '/replacement':
            return web.Response(status=200 if replacement == 'challenge' else 500,
                headers={'cf-mitigated': 'challenge'} if replacement == 'challenge' else {},
                content_type='text/html', text='<main><h1>Replacement interstitial</h1><p>Not source evidence.</p></main>')
        return web.Response(content_type='text/html', text='''<main><h1>Public discussion</h1>
        <section id="comments"><p>Initial discussion.</p></section>
        <button type="button" aria-controls="comments" onclick="location.href='/replacement'">Load more comments</button></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(options,
            deadline=5)) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
        assert source.get('error', {}).get('code') == expected
        assert 'content' not in source and not source.get('highlights')
        cached = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'ordinary'}))['sources'][0]
        assert 'error' in cached and 'content' not in cached


async def test_successful_expansion_navigation_does_not_restart_action_budget(tmp_path):
    from dataclasses import replace
    visits = []
    async def handler(request):
        visits.append(request.path)
        next_path = '/second' if request.path == '/' else '/third'
        return web.Response(content_type='text/html', text=f'''<main><h1>Discussion {request.path}</h1>
        <section id="comments"><p>Public replacement discussion.</p></section>
        <button type="button" aria-controls="comments" onclick="location.href='{next_path}'">Load more comments</button></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(options,
            expansion_actions=1)) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
        assert 'error' not in source
        assert source['resolved_url'] == base + '/second'
        assert visits == ['/', '/second']
        assert source['coverage']['actions'] == 1
        assert source['coverage']['partial']


async def test_public_text_and_expansion_survive_a_never_idle_native_resource(tmp_path):
    import asyncio
    finished = asyncio.Event()
    async def handler(request):
        if request.path == '/events':
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            await response.write(b'data: connected\n\n')
            await finished.wait()
            return response
        return web.Response(content_type='text/html', text='''<main><h1>Live discussion</h1>
        <section id="comments"><p>Initial public discussion.</p></section>
        <button type="button" aria-controls="comments" onclick="document.querySelector('#comments').insertAdjacentHTML('beforeend', '<p>Warning: uncancel cannot restore work.</p>');this.remove()">Load more comments</button>
        <p id=rendered></p><script>fetch('/events').then(r => r.text());setTimeout(() => document.querySelector('#rendered').textContent='Delayed rendered cancellation qualification.', 1200)</script></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
        try:
            source = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert 'error' not in source, source
            assert 'Warning: uncancel cannot restore work.' in source['content']
            assert 'Delayed rendered cancellation qualification.' in source['content']
            assert source['coverage']['partial']
        finally:
            finished.set()


async def test_labelled_discussion_list_continuation_waits_for_loaded_items(tmp_path):
    import asyncio
    async def handler(request):
        if request.path == '/older':
            await asyncio.sleep(2.3)
            return web.Response(text='<li><article><h3>Bob</h3><p>Warning: cancel_called cannot restore work.</p><pre>TaskGroup._abort()</pre></article></li>')
        return web.Response(content_type='text/html', text='''<main><h1>Discussion</h1><h2 id="activity">Activity</h2>
        <ol role="list" aria-labelledby="activity"><li><article><h3>Alice</h3><time datetime="2026-01-01">Jan 1</time><p>Initial discussion.</p></article></li>
        <li id="continuation"><h3 id="remaining">66 remaining items</h3><button type="button" aria-describedby="remaining" onclick="load(this)">Load more</button></li></ol>
        <script>async function load(button) {button.disabled=true;button.textContent='Loading...';
        const text=await fetch('/older').then(r=>r.text());button.remove();
        setTimeout(() => {document.querySelector('#continuation').insertAdjacentHTML('beforebegin', text);document.querySelector('#continuation').remove()}, 250);}</script></main>''')
    async with site(handler) as (base, options):
        discovery = cli_response(json.dumps({'data': [{'t': 0, 'rank': 1, 'url': base, 'title': 'Discussion'}]}))
        async with running(tmp_path, acquisition_options=options, source=discovery) as (url, _), client(url, timeout=60) as session:
            fetched = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert 'error' not in fetched, fetched
            assert 'Warning: cancel_called cannot restore work.' in fetched['content']
            assert fetched['content'].index('Alice') < fetched['content'].index('Bob')
            searched = body(await session.call_tool('search', {'query': 'cancel_called', 'num_results': 1, 'freshness': 'fresh_only'}))['results'][0]
            assert any('Warning: cancel_called cannot restore work.' in h['text'] for h in searched['highlights'])


@pytest.mark.parametrize('change', ['unlabelled', 'account_list', 'external_description', 'no_remaining_count', 'single_item', 'submit', 'form', 'activity_lookalike'])
async def test_continuation_label_does_not_authorize_without_safe_structure(tmp_path, change):
    markup = '''<main><h1>Public discussion</h1><h2 id="activity">Activity</h2>
    <ol aria-labelledby="activity"><li><article><h3><a rel="author">Alice</a></h3><p>Initial qualified discussion.</p></article></li>
    <li><h3 id="remaining">66 remaining items</h3><button type="button" aria-describedby="remaining"
    onclick="document.querySelector('main').append('Unsafe action executed')">Load more</button></li></ol></main>'''
    if change == 'activity_lookalike':
        markup = markup.replace('<li><article><h3><a rel="author">Alice</a></h3><p>Initial qualified discussion.</p></article></li>', '<li><h3>Backup activity</h3><p>Restore account data.</p></li>')
    elif change == 'unlabelled':
        markup = markup.replace('aria-labelledby="activity"', '')
    elif change == 'account_list':
        markup = markup.replace('>Activity<', '>Account<')
    elif change == 'external_description':
        markup = markup.replace('<h3 id="remaining">66 remaining items</h3>', '').replace('<ol ', '<h3 id="remaining">66 remaining items</h3><ol ')
    elif change == 'no_remaining_count':
        markup = markup.replace('66 remaining items', 'Account actions')
    elif change == 'single_item':
        markup = markup.replace('<li><article><h3><a rel="author">Alice</a></h3><p>Initial qualified discussion.</p></article></li>', '')
    elif change == 'submit':
        markup = markup.replace('type="button"', 'type="submit"')
    elif change == 'form':
        markup = markup.replace('<button ', '<form><button ').replace('</button>', '</button></form>')
    async def handler(request):
        return web.Response(content_type='text/html', text=markup)
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'Unsafe action executed' not in source['content']
        assert source['coverage']['partial']
        assert source['coverage']['stop_reason'] == 'unsupported_controls'


@pytest.mark.parametrize('replacement, expected', [('challenge', 'fetch_timeout'), ('error', 'fetch_failed')])
async def test_late_replacement_during_persistent_resource_settling_is_not_cached(tmp_path, replacement, expected):
    from retrieval_helpers import running, client
    import asyncio
    from dataclasses import replace
    finished = asyncio.Event()
    async def handler(request):
        if request.path == '/events':
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'pending')
            await finished.wait()
            return response
        if request.path == '/replacement':
            return web.Response(content_type='text/html', status=200 if replacement == 'challenge' else 500,
                headers={'cf-mitigated': 'challenge'} if replacement == 'challenge' else {},
                text='<h1>Replacement must not become evidence.</h1>')
        return web.Response(content_type='text/html', text='''<main><h1>Initial must not become evidence.</h1></main>
        <script>fetch('/events').then(r=>r.text());setTimeout(()=>location.href='/replacement',1200)</script>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(options, deadline=5)) as (url, _), client(url, timeout=60) as session:
        try:
            source = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert source['error']['code'] == expected, source
            cached = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only'}))['sources'][0]
            assert cached['error']['code'] == 'cache_miss'
        finally:
            finished.set()


async def test_initial_substantive_fetch_settles_beside_unrelated_persistent_fetch(tmp_path):
    import asyncio
    finished = asyncio.Event()
    async def handler(request):
        if request.path == '/telemetry':
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'pending')
            await finished.wait()
            return response
        if request.path == '/content':
            await asyncio.sleep(2.7)
            return web.Response(text='Warning: late rendered cancellation qualification.')
        return web.Response(content_type='text/html', text='''<main><h1>Rendered public reference</h1><p id=content>Loading public content...</p></main>
        <script>fetch('/telemetry').then(r=>r.text());fetch('/content').then(r=>r.text()).then(t=>document.querySelector('#content').textContent=t)</script>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
        try:
            source = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert 'Warning: late rendered cancellation qualification.' in source['content']
            assert source['coverage']['initial_render']['partial']
            assert source['coverage']['partial']
        finally:
            finished.set()


@pytest.mark.parametrize('keeps_mutating', [False, True])
async def test_network_idle_timer_rendering_preserves_warning_and_exact_coverage(tmp_path, keeps_mutating):
    async def handler(request):
        return web.Response(content_type='text/html', text=f'''<title>Timer qualification</title>
        <main><h1>Timer qualification</h1><p id="warning">Loading qualification.</p></main>
        <script>
        let ticks = 0;
        const timer = setInterval(() => {{
            ticks++;
            document.querySelector('#warning').textContent = ticks < 12
                ? 'Rendering qualification ' + ticks
                : 'Warning: timer_cancel remains unsafe before version 3.13. Revision ' + ticks;
            if (ticks >= 12 && {str(not keeps_mutating).lower()}) clearInterval(timer);
        }}, 100);
        </script>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
        data = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))
        source = data['sources'][0]
        assert 'error' not in source, source
        assert 'Warning: timer_cancel remains unsafe before version 3.13.' in source['content']
        assert 'Loading qualification.' not in source['content']
        assert source['coverage'] == {
            'exhaustive': False, 'partial': keeps_mutating, 'actions': 0,
            'policy': 'bounded_read_only_in_page_controls',
            'stop_reason': 'render_settling_limit' if keeps_mutating else 'settled',
            'initial_render': {'network_idle': True, 'dom_quiet': not keeps_mutating,
                               'pending_native_load': False, 'partial': keeps_mutating},
        }
        assert data['budget']['partial_coverage'] is keeps_mutating
        cached = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'ordinary'}))['sources'][0]
        assert cached['content'] == source['content']
        assert cached['content_version'] == source['content_version']
        assert cached['coverage'] == source['coverage']


async def test_adapter_authorization_cannot_bypass_generic_guards(tmp_path, monkeypatch):
    from vresto import sites
    # Deliberately overbroad authorization: the shared loop, not the adapter,
    # must enforce every read-only restriction.
    monkeypatch.setattr(sites, 'ADAPTERS', (sites.SiteAdapter('broad', lambda url: True,
        expansion=lambda: sites.Expansion(authorize_selector='*')),))
    bad = "document.querySelector('#content').append(' UNSAFE_CLICK');return false"
    anchor = f'<a href="#" aria-controls="content" onclick="{bad}">Show 1 more comment</a>'
    forbidden = ''.join(f'<{tag}>{anchor}</{tag}>' for tag in ('form', 'nav', 'dialog'))
    forbidden += f'<div role="dialog">{anchor}</div><div role="navigation">{anchor}</div>'
    forbidden += anchor.replace('href="#"', 'href="/navigation"')
    forbidden += anchor.replace('href="#"', 'href="javascript:void(0)"')
    forbidden += anchor.replace('href="#"', 'href="https://example.org/#fragment"')
    forbidden += anchor.replace('aria-controls="content"', 'aria-controls="missing"')
    forbidden += anchor.replace('aria-controls="content"', 'aria-controls="outside"')
    forbidden += f'<div hidden>{anchor}</div>'
    forbidden += f'<button type="submit" aria-controls="content" onclick="{bad}">Show more</button>'
    forbidden += f'<button type="button" form="missing" aria-controls="content" onclick="{bad}">Show more</button>'
    forbidden += anchor.replace('aria-controls="content"', 'aria-controls="content" aria-busy="true"')
    async def handler(request):
        return web.Response(content_type='text/html', text=f'''<div id="outside">Outside scope.</div>
        {anchor}<main><h1>Bounded discussion</h1><section id="content">Initial content.</section>
        {forbidden}
        <a href="#comments" aria-controls="content" onclick="document.querySelector('#content').append(' Authorized comment.');this.remove();return false">Show 1 more comment</a>
        <a href="" aria-controls="content" onclick="document.querySelector('#content').append(' Empty href comment.');this.remove();return false">Show 1 more comment</a>
        </main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'error' not in source, source
        assert 'UNSAFE_CLICK' not in source['content']
        assert 'Authorized comment.' in source['content']
        assert 'Empty href comment.' in source['content']
        assert source['coverage']['actions'] == 2
        assert source['coverage']['partial'] is True
