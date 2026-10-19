"""Main-content behavior at the public HTTP MCP seam, using native fixture pages."""
import pytest
from aiohttp import web
from test_fetch import site
from test_http import running, client, body


async def test_documentation_budget_spent_on_technical_content_not_navigation(tmp_path):
    async def handler(request):
        menu = ''.join(f'<a href="/menu/{i}">Unrelated menu destination {i}</a>' for i in range(80))
        return web.Response(content_type='text/html', text=f'''<title>Reference</title>
        <nav aria-label="Global navigation"><h2>Navigation Menu</h2>{menu}</nav>
        <main><h1>connect_safe reference</h1><p>Only version 2.4 supports connect_safe.
        Warning: do not enable retries in version 2.3.</p>
        <pre><code>def connect_safe():
    return "safe"</code></pre>
        <table><tr><th>Option</th><th>Default</th></tr><tr><td>retry</td><td>0</td></tr></table>
        <p>See note <a href="#note">1</a>.</p></main>
        <aside><p>Version constraint: this does not apply to Windows.</p></aside>
        <footer><nav><a href="/privacy">Privacy menu</a><a href="/about">About menu</a></nav>
        <p id="note">1. Do not use connect_safe with unsupported transports.</p>
        <p>Copyright Example Authors. Licensed under Apache-2.0.</p></footer>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options, response_budget_bytes=4096) as (url, _), client(url) as session:
        result = await session.call_tool('fetch', {'urls': [base]})
        assert not result.isError, body(result)
        source = body(result)['sources'][0]
        assert 'error' not in source, source
        content = source['content']
        assert 'Unrelated menu' not in content and 'Navigation Menu' not in content
        for expected in ['# connect_safe reference', 'Only version 2.4', 'do not enable retries in version 2.3',
                         '    return "safe"', '|', 'retry', '0', 'does not apply to Windows',
                         'Do not use connect_safe with unsupported transports', 'Apache-2.0']:
            assert expected in content
        assert 'Privacy menu' not in content and 'About menu' not in content
        assert 'content' not in source.get('truncated_fields', [])
        assert len(result.model_dump_json(exclude_none=True).encode()) <= 4096


@pytest.mark.parametrize('kind', ['question_answer', 'issue_discussion'])
async def test_discussion_preserves_answers_timeline_and_removes_repeated_controls(tmp_path, kind):
    async def handler(request):
        content = ('''<h1>Why does connect_safe reject retries?</h1>
            <article><h2>Question by Alice</h2><p>Does connect_safe support retries in 2.3?</p></article>
            <article><h2>Accepted answer by Bob</h2><p>No. connect_safe requires 2.4;
            it does not support retries in 2.3.</p></article>''' if kind == 'question_answer' else '''
            <h1>connect_safe issue #42</h1><article><h2>Reported by Alice</h2>
            <p>connect_safe fails with retries on 2.3.</p></article>
            <article><h2>Maintainer Bob: resolved in 2.4</h2><p>Do not backport this fix to 2.3.
            Version 2.4 is required; the compatibility limitation remains.</p></article>''')
        controls = '<div role="toolbar"><h3>Metadata controls</h3><button>Copy link</button><button>Subscribe</button></div>'
        return web.Response(content_type='text/html', text=f'''<nav><a href="/issues">Repository navigation</a></nav>
            <main>{controls}{content}{controls}</main>
            <footer role="navigation"><a href="/terms">Footer navigation</a><a href="/help">Help menu</a></footer>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options, response_budget_bytes=4096) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        content = source['content']
        for clutter in ['Repository navigation', 'Footer navigation', 'Metadata controls', 'Copy link', 'Subscribe']:
            assert clutter not in content
        for retained in ['Alice', 'Bob', 'connect_safe', '2.3', '2.4']:
            assert retained in content
        assert ('does not support retries' if kind == 'question_answer' else 'Do not backport') in content


async def test_heading_poor_page_falls_back_without_erasing_uncertain_qualifications(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<title>Compatibility note</title>
            <div><p>connect_safe works on version 2.4 only. It does not support retries.</p>
            <pre><code>if ready:
    connect_safe()</code></pre></div>
            <nav><p>Warning: do not deploy on Windows.</p><a href="/constraint">Platform constraint</a></nav>
            <div><a href="/appendix">Technical appendix</a><a href="/errata">Errata</a></div>
            <footer><a href="/privacy">Privacy menu</a><a href="/contact">Contact menu</a></footer>
            <p>Written by Carol. Licensed under MIT.</p>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        content = source['content']
        assert 'Privacy menu' not in content and 'Contact menu' not in content
        for retained in ['version 2.4 only', 'does not support retries', '    connect_safe()',
                         'do not deploy on Windows', 'Platform constraint', 'Technical appendix', 'Errata', 'Carol', 'MIT']:
            assert retained in content


async def test_search_fetch_share_canonical_offsets_and_old_ids_expand_original_version(tmp_path):
    import json
    revision = 'legacy'
    visits = []
    async def handler(request):
        visits.append(request.path)
        # Unknown boundaries deliberately survive. When the publisher identifies
        # the same UI as navigation, only new acquisitions may clean it away.
        tag = 'div' if revision == 'legacy' else 'nav'
        return web.Response(content_type='text/html', text=f'''<title>Reference</title>
            <{tag}><h2>Old UI</h2><a href="/home">Legacy navigation control</a></{tag}>
            <main><h1>connect_safe</h1><p>connect_safe requires version 2.4 only.
            Do not use retries in 2.3.</p></main>''')
    async with site(handler) as (base, options):
        cli = 'import json, sys\nif "--version" in sys.argv:\n print("kagi 0.22.0")\nelse:\n print(' + repr(json.dumps({'data': [
            {'t': 0, 'rank': 1, 'url': base, 'title': 'Reference', 'snippet': 'Provider summary'}]})) + ')\n'
        async with running(tmp_path, acquisition_options=options, source=cli) as (url, _), client(url) as session:
            original = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
            old_selected = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
            old_quote = old_selected['highlights'][0]
            assert 'Legacy navigation control' in original['content']
            revision = 'semantic'
            current = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'fresh_only'}))['sources'][0]
            assert 'Legacy navigation control' not in current['content']
            assert current['content_version'] != original['content_version']
            fetched = body(await session.call_tool('fetch', {'urls': [base], 'query': 'connect_safe'}))['sources'][0]
            searched = body(await session.call_tool('search', {'query': 'connect_safe', 'num_results': 1}))['results'][0]
            assert searched['content_version'] == fetched['content_version'] == current['content_version']
            assert searched['highlights'] == fetched['highlights']
            for quote in fetched['highlights']:
                assert current['content'][quote['start']:quote['end']] == quote['text']
                assert 'requires version 2.4 only' in quote['text'] and 'Do not use retries in 2.3' in quote['text']
            expanded = body(await session.call_tool('expand', {'evidence_ids': [old_quote['evidence_id']]}))['sources'][0]
            assert expanded['content_version'] == original['content_version']
            assert expanded['highlights'] == [old_quote]
            assert 'Legacy navigation control' in str(expanded['context'])
            assert visits == ['/', '/']
        async with running(tmp_path, acquisition_options=options, source=cli) as (url, _), client(url) as session:
            retained = body(await session.call_tool('expand', {'evidence_ids': [old_quote['evidence_id']]}))['sources'][0]
            assert retained['highlights'] == [old_quote]
            assert retained['content_version'] == original['content_version']
            assert 'Legacy navigation control' in str(retained['context'])
            assert visits == ['/', '/']


async def test_link_qualifications_in_semantic_navigation_are_not_clutter(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Compatibility</h1>
            <p>connect_safe has platform limitations.</p></main>
            <nav><h2>Not supported on Windows</h2><a href="/limits">Never use legacy transports</a></nav>
            <footer><a href="#note1">Note 1</a><a href="#note2">Note 2</a></footer>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        content = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]['content']
        for qualification in ['Not supported on Windows', 'Never use legacy transports', 'Note 1', 'Note 2']:
            assert qualification in content


@pytest.mark.parametrize('heading', ['', '<h1>Reference index</h1>'])
async def test_navigation_only_index_has_conservative_original_document_fallback(tmp_path, heading):
    async def handler(request):
        return web.Response(content_type='text/html', text=f'''<title>Reference index</title>{heading}
            <nav><a href="/reference">Reference manual</a><a href="/api">API index</a></nav>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        source = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        assert 'error' not in source, source
        assert 'Reference manual' in source['content'] and 'API index' in source['content']


async def test_link_only_footer_keeps_explicit_author_and_license_attribution(tmp_path):
    async def handler(request):
        return web.Response(content_type='text/html', text='''<main><h1>Reference</h1>
            <p>connect_safe is documented here.</p></main><footer>
            <a rel="author" href="/alice">Alice Example</a>
            <a rel="license" href="/license">MIT</a></footer>''')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _), client(url) as session:
        content = body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]['content']
        assert 'Alice Example' in content and 'MIT' in content


async def test_health_reports_coarse_state_without_warming_or_leaking(tmp_path):
    import httpx
    async def handler(request):
        return web.Response(content_type='text/html', text='<h1>Health fixture</h1><p>Public text.</p>')
    async with site(handler) as (base, options), running(tmp_path, acquisition_options=options) as (url, _):
        health_url = url.removesuffix('/mcp') + '/health'
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.get(health_url)).status_code == 401
            before = (await http.get(health_url, headers={'Authorization': 'Bearer client-secret'})).json()
        assert before['status'] == 'ok' and before['browser'] == 'stopped'
        assert before['cache']['entries'] == 0
        async with client(url) as session:
            assert 'error' not in body(await session.call_tool('fetch', {'urls': [base]}))['sources'][0]
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.get(health_url, headers={'Authorization': 'Bearer client-secret'})
        after = response.json()
        assert after['browser'] == 'running' and after['cache']['entries'] == 1
        assert after['acquisition'] == {'active': 0, 'waiting': 0}
        assert type(after['acquisition']['active']) is int
        assert base not in response.text and 'secret' not in response.text
