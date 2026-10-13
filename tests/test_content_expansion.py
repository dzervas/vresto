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
            assert data['acquisition_limits']['expansion_seconds'] == 2
            quote = original['highlights'][0]
            version = '3.14'
            current = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert 'Additional differing qualification in version 3.14.' in current['content']
            assert current['content_version'] != original['content_version']
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
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
# occur in its expanded discussion. No fixture/private-destination exception,
# credentials, Python downloads or caller JS are supplied to acquisition.
# KAGI_CRAWLER_EXPANSION_SMOKE_URL=https://... \
# KAGI_CRAWLER_EXPANSION_SMOKE_EXPECTED='qualified loaded text' pytest -q ...
@pytest.mark.skipif(not os.environ.get('KAGI_CRAWLER_EXPANSION_SMOKE_URL'), reason='authorized public smoke is opt-in')
async def test_opt_in_public_expansion_fetch_and_composed_search(tmp_path):
    target = os.environ['KAGI_CRAWLER_EXPANSION_SMOKE_URL']
    expected = os.environ['KAGI_CRAWLER_EXPANSION_SMOKE_EXPECTED']
    source = cli_response(json.dumps({'data': [{'t': 0, 'rank': 1, 'url': target, 'title': 'Public discussion'}]}))
    async with running(tmp_path, source=source) as (url, _), client(url, timeout=60) as session:
        fetched = body(await session.call_tool('fetch', {'urls': [target], 'freshness': 'fresh_only',
            'response_budget_bytes': 262144}))['sources'][0]
        assert 'error' not in fetched
        assert expected in fetched['content']
        searched = body(await session.call_tool('search', {'query': expected, 'num_results': 1,
            'response_budget_bytes': 262144}))['results'][0]
        assert searched['content_version'] == fetched['content_version']
        assert expected in ' '.join(h['text'] for h in searched['highlights'])
        assert fetched['coverage']['exhaustive'] is False


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
        <button type="button" aria-controls="comments">Load more comments</button></main>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=replace(options,
            request_timeout=1)) as (url, _), client(url) as session:
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


@pytest.mark.parametrize('replacement, expected', [('challenge', 'blocked'), ('error', 'fetch_failed')])
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
            request_timeout=2)) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
        assert source.get('error', {}).get('code') == expected
        assert 'content' not in source and not source.get('highlights')
        cached = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'cache_only'}))['sources'][0]
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
