"""Pure extraction regressions through the same extraction interface as fetch.

No browser processes: native HTTP/lifecycle suites are serialized separately.
"""
from datetime import datetime, timezone
import hashlib
import re

from lxml import html

from kagi_crawler.budget import apply_budget, wire_bytes
from kagi_crawler.cache import DocumentCache
from kagi_crawler.evidence import evidence_id, expand_evidence, sections
from kagi_crawler.extraction import extract
from kagi_crawler.normalization import main_content_html


BASE = 'https://example.org/reference'


def markdown(document):
    return extract(document, BASE).markdown


def definition(name, body):
    return (f'<dl class="py method"><dt class="sig sig-object py" id="Task.{name}">'
            f'<span class="sig-prename">Task.</span><span class="sig-name">{name}</span>()'
            f'<a class="headerlink" href="#Task.{name}" title="Link to this definition">¶</a>'
            f'</dt><dd>{body}</dd></dl>')


def test_semantic_controls_removed_without_technical_content_loss():
    document = '''<h1>Task reference<a href="#tasks" title="Link to this heading">¶</a></h1>
    <nav><a href="/home">Site navigation</a></nav>
    <div role="toolbar"><button>Copy link</button><button>Subscribe</button></div>
    <article><header><a href="/alice" rel="author">Alice Example</a> on
    <time datetime="2026-10-14">14 October 2026</time>
    <img role="presentation" src="/avatar.png" alt="Alice avatar"></header>
    <p>TaskGroup does not swallow CancelledError. Also call uncancel.</p>
    <button aria-label="Add reaction">Thumbs up 12</button><span role="button">Reply</span>
    <select><option>Newest first</option><option>Oldest first</option></select>
    <pre><code>if ready:
    uncancel()
    # Copy link is an example, not UI
    print("Subscribe")</code></pre>
    <table><tr><th>Operation</th><th>Count</th></tr><tr><td>uncancel</td><td>0</td></tr></table>
    <p>See <a href="#note1">footnote 1</a> and <a href="/constraints">compatibility details</a>.</p>
    </article><aside><p>Warning: this does not apply to Windows.</p></aside>
    <footer><p id="note1">1. Never suppress cancellation without cleanup.</p>
    <p>Copyright Alice Example. Licensed under Apache-2.0.</p></footer>'''
    content = markdown(document)
    for clutter in ['Site navigation', 'Thumbs up 12', 'Reply', 'Newest first', 'Oldest first', 'Alice avatar', '¶']:
        assert clutter not in content
    assert content.count('Copy link') == 1 and content.count('Subscribe') == 1
    for retained in ['Alice Example', '14 October 2026', 'does not swallow CancelledError',
                     '    uncancel()', '    # Copy link', '    print("Subscribe")',
                     'footnote 1',
                     '/constraints', 'does not apply to Windows',
                     'Never suppress cancellation without cleanup', 'Apache-2.0']:
        assert retained in content
    assert re.search(r'\| Operation\s+\| Count\s+\|', content)
    assert re.search(r'\| uncancel\s+\| 0\s+\|', content)
    assert '##' in content and 'Alice Example' in next(line for line in content.splitlines() if line.startswith('##'))


def test_non_element_nodes_in_controls_and_navigation_preserve_protected_labels():
    for marker in ['<!-- layout icon -->', '<?layout icon?>']:
        document = f'''<h1>Task reference</h1><p>Public cancellation reference.</p>
        <nav>{marker}<a href="/home">Ordinary menu</a></nav>
        <button>{marker}Copy link</button><div role="button">{marker}Subscribe</div>
        <nav>{marker}<a rel="author" href="/alice">Alice</a><a href="/help">Help</a></nav>
        <button>{marker}<time>14 October 2026</time></button>
        <button>{marker}<span>Do not suppress cancellation without cleanup</span></button>
        <div role="toolbar">{marker}<a rel="license" href="/license">MIT</a></div>'''
        content = markdown(document)
        for retained in ['Alice', '14 October 2026', 'Do not suppress cancellation without cleanup', 'MIT']:
            assert retained in content
        for clutter in ['Ordinary menu', 'Copy link', 'Subscribe', 'layout icon']:
            assert clutter not in content


def test_words_and_css_coincidences_are_not_ui_evidence():
    document = '''<h1>Controls and reactions</h1>
    <div class="toolbar reaction avatar permalink"><p>Copy link and Subscribe are documented actions.</p>
    <a href="/reactions">Reaction API</a><img src="/diagram.png" alt="Avatar protocol diagram"></div>
    <p>The UI displays <button>Retry</button> after a failure.</p>
    <blockquote><button>Copy link</button> is the exact label.</blockquote>
    <pre><code>&lt;button&gt;Subscribe&lt;/button&gt;</code></pre>
    <table><tr><td><button>Cancel</button></td><td>Operation label</td></tr></table>
    <nav><a href="/limits">Not supported on Windows</a></nav>
    <button>Warning: do not retry</button>
    <button>Cancellation propagates to unfinished tasks.</button>
    <h2>Footnote reference<a href="#note">§</a></h2>
    <div role="button"><p>Only version 2.4 supports this action.</p></div>
    <a href="#note">¶</a><p id="note">¶ is the footnote label.</p>
    <div class="sig sig-object" id="fake"><span class="sig-name">Not an API definition</span></div>
    <dl><dt class="sig sig-object" id="glossary">Reaction</dt><dd>Ordinary glossary term.</dd></dl>'''
    normalized = main_content_html(document)
    content = markdown(document)
    for retained in ['Copy link and Subscribe', 'Reaction API', 'Avatar protocol diagram', 'Retry',
                     'exact label', '<button>Subscribe</button>', 'Cancel', 'Not supported on Windows',
                     'Warning: do not retry', 'Cancellation propagates to unfinished tasks.',
                     'Only version 2.4', '¶', '§', 'Ordinary glossary term']:
        assert retained in content
    root = html.fromstring(normalized)
    assert root.xpath('//dt[@id="glossary"]')
    assert root.xpath('//div[@id="fake"]')


def test_api_definitions_localize_unqualified_parent_without_losing_signature_warning_or_version():
    background = ''.join(definition(f'operation_{i}', '<p>' + 'Ordinary scheduler details. ' * 50 + '</p>')
                         for i in range(14))
    document = ('<h1>Asyncio reference</h1><h2>Task Object</h2><p>Task object operation reference.</p>' +
                background + definition('uncancel', '<p>Decrement the cancellation count.</p>'
                    '<h4>Warning</h4><p>Do not swallow CancelledError unless also calling uncancel.</p>'
                    '<p>Changed in version 3.12: applies to the documented release.</p>') +
                definition('done', '<p>Return whether the task has completed.</p>'))
    content = markdown(document)
    assert len(content) > 16500
    matches = [(a, b, title) for a, b, title in sections(content) if title == 'Task.uncancel()']
    assert matches
    for start, end, _ in matches:
        quote = content[start:end]
        assert len(quote) < 1200
        assert 'Task.uncancel()' in quote and 'Decrement the cancellation count' in quote
        assert 'Do not swallow CancelledError' in quote and 'Changed in version 3.12' in quote
        assert 'operation_0' not in quote and 'Task.done' not in quote
    assert '¶' not in content
    # Fixed public wire envelope, including the single structuredContent copy
    # serialization and an exact version-bound reference, not just quote bytes.
    start, end, title = matches[0]
    source = {'requested_url': BASE, 'resolved_url': BASE, 'content': content,
              'content_version': hashlib.sha256(content.encode()).hexdigest()}
    quote = {'url': BASE, 'section': title, 'start': start, 'end': end,
             'text': content[start:end], 'evidence_id': evidence_id(source, start, end)}
    bounded = apply_budget({'sources': [{'requested_url': BASE, 'resolved_url': BASE,
        'content_version': source['content_version'], 'content_mode': 'highlights',
        'highlights': [quote]}]}, 4096)
    assert wire_bytes(bounded) <= 4096
    assert bounded['sources'][0]['highlights'] == [quote]
    assert not bounded['budget']['partial_coverage']


def test_nested_api_definitions_preserve_qualified_class_scope():
    document = ('<h1>Reference</h1><h2>Objects</h2><dl class="py class">'
                '<dt class="sig sig-object" id="Task"><span class="sig-name">Task</span>(coro)</dt>'
                '<dd><p>This object is not thread-safe.</p>' +
                definition('cancel', '<p>Request cancellation.</p>') +
                definition('uncancel', '<p>Reduce cancellation count. Never swallow CancelledError.</p>') +
                '</dd></dl>')
    content = markdown(document)
    assert re.search(r'^###\s+Task\(coro\)$', content, re.M) and '####' in content
    match = next((a, b) for a, b, title in sections(content) if title == 'Task.uncancel()')
    quote = content[match[0]:match[1]]
    assert 'not thread-safe' in quote and 'Task(coro)' in quote
    assert 'Request cancellation' in quote  # Contiguous canonical scope, not stitched text.
    assert 'Never swallow CancelledError' in quote


def test_discussion_boundaries_use_visible_attribution_in_order():
    document = '''<h1>Cancellation discussion</h1>
    <article><header><a rel="author" href="/alice">Alice</a> — <time>2026-10-12</time></header>
    <p>Does cancellation support retries?</p><button>Reply</button></article>
    <article><header><a rel="author" href="/bob">Bob</a> — <time>2026-10-13</time></header>
    <p>No. Do not retry after CancelledError.</p>
    <article><header><a rel="author" href="/carol">Carol</a> — <time>2026-10-14</time></header>
    <p>Also call uncancel when suppressing cancellation.</p></article></article>
    <article><header><a rel="author" href="/dana">Dana</a> — <time>2026-10-15</time></header>
    <p>Thanks for the explanation.</p></article>'''
    content = markdown(document)
    labels = [title for _, _, title in sections(content) if title]
    assert [label.split(']')[0].split('[')[-1] for label in labels[1:]] == ['Alice — 2026-10-12', 'Bob — 2026-10-13', 'Carol — 2026-10-14', 'Dana — 2026-10-15']
    carol = next(content[a:b] for a, b, title in sections(content) if title and title.startswith('Carol'))
    assert 'Do not retry after CancelledError' in carol  # Bob's dated parent scope survives.
    assert 'Alice' not in carol and 'Dana' not in carol
    assert 'Reply' not in content


def test_ambiguous_article_header_and_unknown_definition_stay_unchanged():
    document = '''<h1>Unknown structure</h1><article><header><p>Prose introduction without attribution.</p></header>
    <p>Warning: do not delete this explanation.</p></article>
    <dl><dt id="uncancel">uncancel</dt><dd><p>This is an ordinary definition list.</p></dd></dl>
    <article><header><a rel="author">Alice</a><p>Long detailed introduction. Warning: do not retry.</p></header></article>'''
    content = markdown(document)
    assert 'Prose introduction without attribution' in content
    assert 'Warning: do not delete' in content and 'ordinary definition list' in content
    assert 'Warning: do not retry' in content
    root = html.fromstring(main_content_html(document))
    assert root.xpath('//dt[@id="uncancel"]')
    assert root.xpath('//article[1]/header')


def test_release_ancestry_and_warning_across_long_explanation_remain_contiguous():
    explanation = '<p>' + 'Cleanup scheduling details ' * 80 + '</p>'
    document = ('<h1>Python 3.12 only</h1><p>Experimental; not available in 3.11.</p>'
                '<h2>Task Object</h2>' + definition('uncancel',
                    '<p>TaskGroup cancellation propagates to unfinished tasks.</p>' + explanation +
                    '<h4>Warning</h4><p>Do not swallow CancelledError; also call uncancel.</p>'))
    content = markdown(document)
    matches = [content[a:b] for a, b, title in sections(content) if title == 'Task.uncancel()']
    assert matches
    for quote in matches:
        assert 'Python 3.12 only' in quote and 'not available in 3.11' in quote
        assert 'Do not swallow CancelledError' in quote
        assert 'Cleanup scheduling details' in quote


def test_new_normalization_does_not_rewrite_retained_versions_or_evidence(tmp_path):
    legacy = markdown('<h1>Reference</h1><div>Legacy UI controls: Copy link, Subscribe</div>'
                      '<h2>uncancel</h2><p>Do not swallow CancelledError.</p>')
    current = markdown('<h1>Reference</h1><button>Copy link</button><button>Subscribe</button>' +
                       definition('uncancel', '<p>Do not swallow CancelledError.</p>'))

    def canonical(content):
        return {'requested_url': BASE, 'resolved_url': BASE, 'content': content,
                'content_version': hashlib.sha256(content.encode()).hexdigest(),
                'document_type': 'html', 'fetched_at': datetime.now(timezone.utc).isoformat()}

    cache = DocumentCache(tmp_path)
    old = canonical(legacy)
    cache.store(old)
    start, end, _ = next(p for p in sections(legacy) if p[2] == 'uncancel')
    identifier = evidence_id(old, start, end)
    snapshots = {path.name: path.read_bytes() for path in tmp_path.glob('*.json')}
    new = canonical(current)
    cache.store(new)
    assert old['content_version'] != new['content_version']
    assert 'Copy link' in legacy and 'Copy link' not in current
    for name, payload in snapshots.items():
        assert (tmp_path / name).read_bytes() == payload
    restarted = DocumentCache(tmp_path)
    assert restarted.lookup(BASE)['content'] == current
    expanded = expand_evidence(restarted, [identifier])['sources'][0]
    assert expanded['content_version'] == old['content_version']
    assert expanded['highlights'][0]['text'] == legacy[start:end]
    assert 'Legacy UI controls' in str(expanded['context'])
    evicted = DocumentCache(tmp_path, cap_bytes=1)
    assert expand_evidence(evicted, [identifier])['sources'][0]['error']['code'] == 'evidence_unavailable'


def test_original_comment_heading_does_not_make_next_attribution_its_child():
    document = '''<h1>Discussion</h1><article><h2>Question by Alice</h2>
    <p>Only version 3.12 supports the original request.</p><h3>Details</h3><p>Request details.</p></article>
    <article><header><a rel="author">Bob</a> <time>2026-10-14</time></header>
    <p>Independent answer about uncancel.</p></article>'''
    content = markdown(document)
    quote = next(content[a:b] for a, b, title in sections(content) if title == 'Bob 2026-10-14')
    assert 'Independent answer' in quote
    assert 'Question by Alice' not in quote and 'original request' not in quote
    assert re.search(r'^##\s+Bob', content, re.M)


def test_heading_depth_and_shared_overload_body_fall_back_without_scope_loss():
    document = ('<h6>Task Object</h6><p>This object is not thread-safe.</p>' +
                definition('uncancel', '<p>Decrement the cancellation count.</p>'))
    root = html.fromstring(main_content_html(document))
    assert root.xpath('//dt[@id="Task.uncancel"]')
    content = markdown(document)
    assert all('not thread-safe' in content[a:b] for a, b, title in sections(content) if title == 'Task Object')
    overloads = '''<h1>Reference</h1><dl><dt class="sig sig-object" id="first">
    <span class="sig-name">uncancel</span>(mode)</dt><dt class="sig sig-object" id="second">
    <span class="sig-name">uncancel</span>()</dt><dd><p>Warning: neither overload is thread-safe.</p></dd></dl>'''
    root = html.fromstring(main_content_html(overloads))
    assert len(root.xpath('//dt')) == 2  # Shared description must not be detached.
    content = markdown(overloads)
    assert 'uncancel(mode)' in content and 'uncancel()' in content
    assert 'neither overload is thread-safe' in content


def test_separate_attribution_blocks_keep_visible_label_spacing():
    document = '''<h1>Discussion</h1><article><header><p><a rel="author" href="/alice">Alice</a></p><p><time>14 October 2026</time></p></header><p>Never retry cancellation.</p></article>'''
    content = markdown(document)
    labels = [title for _, _, title in sections(content) if title]
    assert labels == ['Discussion', 'Alice 14 October 2026']
    assert 'Never retry cancellation.' in content


def test_interactive_attribution_and_timestamps_are_not_controls():
    document = '''<h1>Reference</h1><p>Task cancellation reference.</p>
    <a role="button" rel="author nofollow" href="/alice">Alice</a>
    <a role="button" rel="license" href="/license">MIT</a>
    <button><a rel="author" href="/bob">Bob</a></button>
    <div role="button"><span rel="license">Apache-2.0</span></div>
    <button><time datetime="2026-10-14">14 October 2026</time></button>
    <div role="button"><time>15 October 2026</time></div>
    <time role="button">16 October 2026</time>
    <nav><a rel="author" role="button" href="/carol">Carol</a><a href="/help">Help</a></nav>
    <div role="toolbar"><button><time>17 October 2026</time></button></div>
    <a role="button" href="/reply">Reply</a><button>Copy link</button>
    <div role="button">Thumbs up 12</div>'''
    content = markdown(document)
    for retained in ['Alice', 'MIT', 'Bob', 'Apache-2.0', 'Carol',
                     '14 October 2026', '15 October 2026', '16 October 2026', '17 October 2026']:
        assert retained in content
    for clutter in ['Reply', 'Copy link', 'Thumbs up 12']:
        assert clutter not in content


def test_control_attribute_constraints_and_navigation_negations_survive():
    document = '''<h1>Reference</h1><p>Task cancellation reference.</p>
    <input type="button" value="No retries without cleanup">
    <input type="submit" value="Cannot retry cancellation">
    <button aria-label="Do not retry cancellation">Retry</button>
    <div role="button" aria-label="Requires cleanup"><input value="Cannot suppress cancellation"></div>
    <nav><input type="button" value="No retries without cleanup"></nav>
    <nav aria-label="Cannot retry cancellation"><a href="/limits">Limits</a></nav>
    <nav><a href="/no">No retries</a></nav>
    <nav><a href="/cannot">Cannot suppress cancellation</a></nav>
    <nav><a href="/without">Unsafe without cleanup</a></nav>
    <button>No retries</button><div role="button">Cannot suppress cancellation</div>
    <button>Unsafe without cleanup</button>
    <input type="button" value="Copy link"><button aria-label="Reply">Reply</button>
    <nav><a href="/home">Ordinary menu</a></nav>'''
    root = html.fromstring(main_content_html(document))
    assert len(root.xpath('//input[@value="No retries without cleanup"]')) == 2
    assert root.xpath('//input[@value="Cannot retry cancellation"]')
    assert root.xpath('//input[@value="Cannot suppress cancellation"]')
    assert root.xpath('//button[@aria-label="Do not retry cancellation"]')
    assert root.xpath('//nav[@aria-label="Cannot retry cancellation"]')
    assert not root.xpath('//input[@value="Copy link"] | //button[@aria-label="Reply"]')
    content = markdown(document)
    for retained in ['No retries', 'Cannot suppress cancellation', 'Unsafe without cleanup', 'Limits', 'Retry']:
        assert retained in content
    assert 'Ordinary menu' not in content


def test_heading_and_bound_accordion_controls_keep_release_and_api_labels():
    document = ('''<h1>Reference</h1><h2><button aria-expanded="true">Python 3.12</button></h2>
        <h3>uncancel</h3><p>Decrement cancellation count.</p>
        <h2><span role="button" aria-expanded="true">Python 3.13</span></h2>
        <h3>cancel</h3><p>Request cancellation.</p>
        <button aria-controls="task-details" aria-expanded="true">Python 3.14</button>
        <section id="task-details"><p>TaskGroup cancellation reference.</p></section>
        <nav><button aria-controls="release-details">Python 3.15</button></nav>
        <section id="release-details"><p>Technical release reference.</p></section>
        <nav><h2><button>Python 3.16</button></h2></nav>
        <h3>done</h3><p>Return whether the task is complete.</p>
        <button>Copy link</button><span role="button">Subscribe</span>''')
    content = markdown(document)
    for label in ['Python 3.12', 'Python 3.13', 'Python 3.14', 'Python 3.15', 'Python 3.16']:
        assert label in content
    quote = next(content[a:b] for a, b, title in sections(content) if title == 'uncancel')
    assert 'Python 3.12' in quote and 'Decrement cancellation count' in quote
    quote = next(content[a:b] for a, b, title in sections(content) if title == 'done')
    assert 'Python 3.16' in quote and 'Return whether the task is complete' in quote
    assert 'Copy link' not in content and 'Subscribe' not in content


async def test_public_http_structural_labels_scopes_and_historical_expansion(tmp_path):
    # Native fixture: included in the parent's serialized integration run only.
    from aiohttp import web
    from test_fetch import site
    from test_http import running, client, body

    release = '3.12'

    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<title>Task reference</title><nav><!-- layout --><a href="/menu">Global menu</a></nav>'
            '<h1>Reference</h1><h2><button aria-expanded="true">Python ' + release + '</button></h2>' +
            definition('uncancel', '<p>Decrement cancellation count.</p><h4>Warning</h4>'
                       '<p>Do not swallow CancelledError without cleanup.</p>') +
            '<button aria-controls="release-details">Release details</button>'
            '<section id="release-details"><p>Compatibility reference for the documented release.</p></section>'
            '<h1>Discussion</h1><article><header><a href="/alice" rel="author">Alice</a> '
            '<time>14 October 2026</time></header><p>No retries without cleanup.</p>'
            '<button>Reply</button><img role="presentation" alt="Alice avatar" src="/avatar.png"></article>'
            '<div role="toolbar"><!-- controls --><button><!-- icon -->Copy link</button>'
            '<button>Subscribe</button></div>'))

    async with site(handler) as (base, options):
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
            full = body(await session.call_tool('fetch', {'urls': [base], 'response_budget_bytes': 65536}))['sources'][0]
            for label in ['Python 3.12', 'Task.uncancel()', 'Release details', 'Alice', '14 October 2026',
                          'No retries without cleanup', 'Do not swallow CancelledError without cleanup']:
                assert label in full['content']
            for clutter in ['Global menu', 'Reply', 'Alice avatar', 'Copy link', 'Subscribe', '¶']:
                assert clutter not in full['content']
            result = await session.call_tool('fetch', {'urls': [base], 'query': 'uncancel',
                                                      'response_budget_bytes': 65536})
            assert len(result.model_dump_json(exclude_none=True).encode()) <= 65536
            selected = body(result)['sources'][0]
            assert selected['highlights']
            quote = selected['highlights'][0]
            assert 'Python 3.12' in quote['text']
            assert 'Do not swallow CancelledError without cleanup' in quote['text']
            assert full['content'][quote['start']:quote['end']] == quote['text']
            release = '3.13'
            refreshed = body(await session.call_tool('fetch', {'urls': [base], 'freshness': 'refresh'}))['sources'][0]
            assert 'Python 3.13' in refreshed['content'] and 'Python 3.12' not in refreshed['content']
            assert refreshed['content_version'] != full['content_version']
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert expanded['highlights'] == [quote]
            assert expanded['content_version'] == full['content_version']
        async with running(tmp_path, acquisition_options=options) as (url, _), client(url, timeout=60) as session:
            retained = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert retained['highlights'] == [quote]
            assert retained['content_version'] == full['content_version']


def test_conversion_keeps_control_labels_literal_fences_and_table_pipes():
    content = markdown('<h1>Reference</h1>'
        '<input type="button" value="No retries without cleanup">'
        '<button aria-label="Do not cancel twice"></button><p>Details.</p>'
        '<pre><code>before\n```\n# not a heading\n```\nafter</code></pre>'
        '<table><tr><th>Expression</th><th>Meaning</th></tr>'
        '<tr><td><code>x | y</code></td><td>OR</td></tr></table>')
    assert 'No retries without cleanup' in content and 'Do not cancel twice' in content
    fence = '````'
    assert f'{fence}\nbefore\n```\n# not a heading\n```\nafter\n{fence}' in content
    assert all(title != 'not a heading' for _, _, title in sections(content))
    assert re.search(r'^\| `x \\\| y` \| OR \|$', content, re.M)
