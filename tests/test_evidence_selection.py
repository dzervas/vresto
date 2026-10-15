"""Deterministic selection, canonical fidelity and actual MCP wire budgets."""
from copy import deepcopy
import hashlib

import pytest

from kagi_crawler import evidence
from kagi_crawler.budget import apply_budget, wire_bytes


def source(text, url='https://example.test/docs'):
    return {'content': text, 'requested_url': url, 'resolved_url': url,
            'content_version': hashlib.sha256(text.encode()).hexdigest(),
            'title': 'Technical reference', 'cache_state': 'hit'}


def slices(monkeypatch, intervals):
    monkeypatch.setattr(evidence, 'document_passages', lambda s: iter(intervals))


@pytest.mark.parametrize('heading', [
    'Add an asyncio.TaskGroup.cancel method #108951',
    'Proposal 108951',
    'Related issue #3.12',
])
def test_issue_numbers_do_not_scope_discussion_highlights(heading):
    text = '# ' + heading + '\n\nState: Closed\n\n'
    for i in range(1, 24):
        text += f'## Comment {i} — contributor — 2023-09-05T19:13:00Z\n\n'
        text += ('Ordinary scheduling discussion. ' * 35 + '\n\n' if i < 23 else
                 'cancel_called and TaskGroup._abort describe cancellation.\n')
    data = evidence.select_highlights({'sources': [source(text)]}, 'cancel_called TaskGroup._abort')
    quotes = data['sources'][0]['highlights']
    assert quotes
    assert all(h['start'] == text.index('## Comment 23') for h in quotes)
    assert all(len(h['text']) < 300 for h in quotes)
    assert all(h['text'] == text[h['start']:h['end']] for h in quotes)


@pytest.mark.parametrize('entry', ['Comment', 'Description'])
def test_attributed_discussion_heading_is_not_a_version_scope(entry):
    heading = f'## {entry} 23 — contributor-v2.4 — 2023-09-05T19:13:00Z\n\n'
    text = '# Discussion\n\n' + heading + 'Ordinary background.\n\n'
    text += '### Implementation\n\ncancel_called reports cancellation.\n'
    text += '## Comment 24 — contributor — 2023-09-06T19:13:00Z\n\nTaskGroup._abort cancels tasks.\n'
    data = evidence.select_highlights({'sources': [source(text)]}, 'cancel_called TaskGroup._abort')
    quotes = data['sources'][0]['highlights']
    assert any('cancel_called' in h['text'] for h in quotes)
    assert any('TaskGroup._abort' in h['text'] for h in quotes)
    assert all(heading not in h['text'] for h in quotes)


@pytest.mark.parametrize('heading', ['Python 3.12', 'v2', 'v2.4', 'Version 2', 'Release 2'])
def test_release_heading_still_scopes_child_highlights(heading):
    text = f'# {heading}\n\nRelease reference.\n\n## Operation\n\ncancel_called reports cancellation.\n'
    data = evidence.select_highlights({'sources': [source(text)]}, 'cancel_called')
    quotes = data['sources'][0]['highlights']
    assert quotes
    assert all(h['start'] == 0 and heading in h['text'] for h in quotes)


def test_marginal_identifiers_displace_overlapping_cancel_called(monkeypatch):
    text = 'cancel_called ' * 60 + 'ordinary implementation.\n'
    boundary = len(text)
    text += 'TaskGroup._abort cancels sibling tasks.\n'
    slices(monkeypatch, [(0, boundary, 'Repeated'), (0, boundary - 5, 'Repeated'),
                         (0, boundary - 10, 'Repeated'), (boundary, len(text), 'Abort')])
    data = evidence.select_highlights({'sources': [source(text)]}, 'cancel_called TaskGroup._abort')
    selected = data['sources'][0]['highlights']
    assert len(selected) == 2
    assert any('cancel_called' in h['text'] for h in selected)
    assert any('TaskGroup._abort' in h['text'] for h in selected)
    assert data['sources'][0]['redundant_highlight_count'] == 2
    assert data['selection']['uncovered_query_terms'] == []
    assert data == evidence.select_highlights({'sources': [source(text)]}, 'cancel_called TaskGroup._abort')


def test_compact_relevance_beats_giant_ballast(monkeypatch):
    text = 'cancel_called ' * 3000 + 'irrelevant background. ' * 3000
    boundary = len(text)
    text += 'cancel_called reports cancellation.\n'
    slices(monkeypatch, [(0, boundary, 'Ballast'), (boundary, len(text), 'Compact')])
    data = evidence.select_highlights({'sources': [source(text)]}, 'cancel_called', 4096)
    assert data['sources'][0]['highlights'][0]['section'] == 'Compact'
    assert data['sources'][0]['selection_omitted_highlight_count'] == 1
    assert wire_bytes(apply_budget(data, 4096)) <= 4096


def test_oversized_top_candidate_is_skipped_for_fitting_alternative(monkeypatch):
    from kagi_crawler import budget
    text = 'api config ' + 'ordinary ballast. ' * 500
    boundary = len(text)
    text += 'api supports retries. Warning: do not swallow CancelledError; call uncancel.\n'
    slices(monkeypatch, [(0, boundary, 'Large'), (boundary, len(text), 'Qualified')])
    attempts = []
    def probe(data, limit):
        attempts.append(data['sources'][0]['highlights'][-1]['section'])
        return apply_budget(data, limit)
    monkeypatch.setattr(budget, 'apply_budget', probe)
    data = evidence.select_highlights({'sources': [source(text)]}, 'api config', 4096)
    assert attempts[0] == 'Large'
    assert data['sources'][0]['highlights'][0]['section'] == 'Qualified'
    assert data['selection']['uncovered_query_terms'] == ['config']
    assert data['selection']['partial_coverage']


def test_budget_fallback_preserves_metadata_duplicates_and_exact_ids(monkeypatch):
    text = 'api api api ' + '界\\\" ' * 2000 + '\n'
    boundary = len(text)
    text += 'api supports retries. Warning: do not swallow CancelledError; call uncancel.\n'
    slices(monkeypatch, [(0, boundary, 'Large'), (boundary, len(text), 'Qualified')])
    original = {'sources': [source(text), source(text, 'https://other.test/docs')],
                'acquisition_limits': {'description': 'Bounded shared request'}, 'traversal': {'omissions': []}}
    before = deepcopy(original)
    data = evidence.select_highlights(original, 'api', 6144)
    assert original == before
    bounded = apply_budget(data, 6144)
    assert bounded['budget']['returned_source_count'] == 2
    assert bounded['budget']['omitted_source_indices'] == []
    assert wire_bytes(bounded) <= 6144
    assert all(s['title'] == original['sources'][i]['title'] for i, s in enumerate(data['sources']))
    quote = data['sources'][0]['highlights'][0]
    assert data['sources'][1]['duplicate_evidence'][0]['duplicate_of'] == quote['evidence_id']
    assert quote['text'] == text[quote['start']:quote['end']]
    assert 'Warning' in quote['text'] and 'uncancel' in quote['text']
    class Cache:
        def lookup(self, url, version):
            return next((s for s in original['sources'] if s['requested_url'] == url and s['content_version'] == version), None)
    for s in data['sources']:
        reference = (s['highlights'] or s['duplicate_evidence'])[0]
        canonical, start, end = evidence.resolve_evidence(Cache(), reference['evidence_id'])
        assert canonical['content'][start:end] == quote['text']


def test_overlap_distinct_negation_and_qualification_survive(monkeypatch):
    text = 'api supports retries.\n' + 'Background. ' * 80
    boundary = len(text)
    text += 'Warning: api must not retry in version 2.3.\n'
    slices(monkeypatch, [(0, boundary, 'Claim'), (0, len(text), 'Warning')])
    data = evidence.select_highlights({'sources': [source(text)]}, 'api')
    assert any('must not retry' in h['text'] for h in data['sources'][0]['highlights'])
    small = {'index': 0, 'version': 'v', 'start': 0, 'end': 100, 'terms': {'api'}, 'qualifications': set()}
    qualified = {**small, 'end': 110, 'qualifications': {'must not retry'}}
    assert not evidence._redundant(qualified, [small])
    assert not evidence._redundant({**small, 'index': 1}, [small])
    assert not evidence._redundant({**small, 'version': 'other'}, [small])


def test_suffix_exact_semantics_and_absent_query():
    text = '# APIs\nother.TaskGroup is available.\n'
    data = evidence.select_highlights({'sources': [source(text)]}, 'asyncio.TaskGroup')
    assert data['sources'][0]['relevance'] == 'no_lexical_match'
    assert data['selection']['uncovered_query_terms'] == ['asyncio.taskgroup']
    assert data['selection']['partial_coverage']
    assert evidence.select_highlights({'sources': [source(text)]}, 'TaskGroup')['sources'][0]['highlights']


def test_source_fairness_before_extras(monkeypatch):
    text = 'api works.\napi retries.\napi schedules.\n'
    slices(monkeypatch, [(0, 11, 'One'), (11, 24, 'Two'), (24, len(text), 'Three')])
    original = {'sources': [source(text), source(text.replace('works', 'fails'), 'https://other.test')]}
    data = evidence.select_highlights(original, 'api', 4096)
    assert all(s['highlights'] for s in data['sources'])
    assert wire_bytes(apply_budget(data, 4096)) <= 4096


def test_pdf_neighbor_window_is_atomic_and_attributed():
    pages = ['Warning: never swallow errors.\n', 'TaskGroup._abort cancels tasks.\n',
             'Call uncancel if suppressing cancellation.\n']
    text = ''.join(pages)
    document = source(text)
    document['document_type'] = 'pdf'
    position = 0
    references = []
    for number, page in enumerate(pages, 1):
        references.append({'page': number, 'start': position, 'end': position + len(page), 'status': 'text'})
        position += len(page)
    document['pdf'] = {'references': references}
    data = evidence.select_highlights({'sources': [document]}, 'TaskGroup._abort', 4096)
    quote = data['sources'][0]['highlights'][0]
    assert quote['text'] == text
    assert quote['pages'] == [1, 2, 3]
    assert document['content'] == text


def test_no_fuzzy_deduplication_across_documents():
    documents = [source('# api\napi is safe.\n'),
                 source('# api\napi is not safe.\n', 'https://other.test')]
    data = evidence.select_highlights({'sources': documents}, 'api')
    assert all(s['highlights'] for s in data['sources'])
    assert data['sources'][0]['highlights'][0]['evidence_id'] != data['sources'][1]['highlights'][0]['evidence_id']


async def test_public_fetch_and_composed_search_fit_equal_wire_budgets(tmp_path):
    import json
    from aiohttp import web
    from test_fetch import site
    from test_http import running, client, body, cli_response

    async def handler(request):
        return web.Response(content_type='text/html', text=(
            '<title>Escaped "界" reference</title><h1>Reference ' + request.path + '</h1>'
            '<h2>Ballast</h2><p>cancel_called TaskGroup._abort ' + '界 &quot; \\ ballast ' * 2000 + '</p>'
            '<h2>Cancellation flag</h2><p>cancel_called indicates that cancellation was requested.</p>'
            '<h2>Abort interface</h2><p>TaskGroup._abort cancels unfinished sibling tasks.</p>'
            '<h3>Warning</h3><p>Do not swallow CancelledError; call uncancel if suppression is desired.</p>'))

    async with site(handler) as (base, options):
        urls = [base + '/one', base + '/two']
        payload = {'data': [{'url': url, 'title': 'Escaped "界" reference', 'rank': i + 1}
                            for i, url in enumerate(urls)]}
        async with running(tmp_path, acquisition_options=options,
                source=cli_response(json.dumps(payload))) as (url, _), client(url, timeout=60) as session:
            canonical = body(await session.call_tool('fetch', {
                'urls': urls, 'response_budget_bytes': 262144}))['sources']
            assert all('error' not in s and 'truncated_fields' not in s for s in canonical)
            for tool, arguments, key in [
                    ('fetch', {'urls': urls, 'freshness': 'cache_only'}, 'sources'),
                    ('search', {'num_results': 2}, 'results')]:
                response = await session.call_tool(tool, {**arguments,
                    'query': 'cancel_called TaskGroup._abort', 'response_budget_bytes': 16384})
                data = body(response)
                assert len(response.model_dump_json(exclude_none=True).encode()) <= 16384
                assert data['budget']['omitted_source_indices'] == []
                assert data['budget']['returned_source_count'] == 2
                assert data['budget']['partial_coverage']
                assert data['selection']['uncovered_query_terms'] == []
                for index, selected in enumerate(data[key]):
                    for field in ('requested_url', 'resolved_url', 'content_version', 'title', 'document_type', 'fetched_at'):
                        assert selected[field] == canonical[index][field]
                    assert 'truncated_fields' not in selected
                    quotes = selected['highlights']
                    assert any('cancel_called' in h['text'] for h in quotes)
                    assert any('TaskGroup._abort' in h['text'] for h in quotes)
                    assert any('uncancel' in h['text'] for h in quotes)
                    assert all(h['section'] != 'Ballast' for h in quotes)
                    for quote in quotes:
                        assert quote['text'] == canonical[index]['content'][quote['start']:quote['end']]
                        expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))
                        assert expanded['sources'][0]['highlights'][0]['text'] == quote['text']


def test_tight_budget_suppression_metadata_cannot_evict_admitted_quote(monkeypatch):
    from kagi_crawler import budget

    text = 'alpha beta ' * 20 + 'ordinary.\n'
    slices(monkeypatch, [(0, len(text), 'Full'), (0, len(text) - 1, 'Overlap'),
                         (0, len(text) - 2, 'Overlap')])
    original = {'sources': [source(text)], 'fixture_metadata': ''}
    # Measure the admitted shape before later redundancy counters are added.
    admitted = evidence.select_highlights(original, 'alpha beta')
    admitted['sources'][0].pop('redundant_highlight_count')
    admitted['sources'][0]['selection_omitted_highlight_count'] = 2
    admitted['selection']['partial_coverage'] = True
    padding = (4096 - wire_bytes(apply_budget(admitted, 4096))) // 2
    assert padding > 0
    original['fixture_metadata'] = admitted['fixture_metadata'] = 'x' * padding
    assert 4094 <= wire_bytes(apply_budget(admitted, 4096)) <= 4096

    rejected_counter_updates = []
    def probe(data, limit):
        bounded = apply_budget(data, limit)
        if data['sources'][0].get('redundant_highlight_count'):
            rejected_counter_updates.append(not bounded['sources'][0]['highlights'])
        return bounded
    monkeypatch.setattr(budget, 'apply_budget', probe)
    selected = evidence.select_highlights(original, 'alpha beta', 4096)
    bounded = apply_budget(selected, 4096)
    assert rejected_counter_updates and all(rejected_counter_updates)
    assert wire_bytes(bounded) <= 4096
    assert bounded['sources'][0]['highlights'] == selected['sources'][0]['highlights']
    assert len(bounded['sources'][0]['highlights']) == 1
    assert selected['sources'][0]['selection_omitted_highlight_count'] == 2
    assert not selected['sources'][0].get('redundant_highlight_count')
    quoted_terms = set(evidence.tokens(bounded['sources'][0]['highlights'][0]['text']))
    assert set(bounded['selection']['covered_query_terms']) <= quoted_terms
    assert bounded['selection']['uncovered_query_terms'] == []
    assert bounded['budget']['partial_coverage']
    assert original['sources'][0]['content'] == text

    # With room for the counters, the final partial flag changes to false;
    # that final metadata shape must also be measured, not appended afterward.
    monkeypatch.setattr(budget, 'apply_budget', apply_budget)
    roomy = evidence.select_highlights(original, 'alpha beta', 4352)
    assert roomy['sources'][0]['redundant_highlight_count'] == 2
    assert roomy['sources'][0]['selection_omitted_highlight_count'] == 0
    assert not roomy['selection']['partial_coverage']
    roomy_bounded = apply_budget(roomy, 4352)
    roomy_bounded.pop('budget')
    assert roomy_bounded == roomy


def test_mandatory_and_unsupported_constraints_survive_overlap(monkeypatch):
    for constraint in ('You must call cleanup.', 'This operation is unsupported.',
                       'Cleanup is mandatory.', 'Cleanup is required.', 'You shall call cleanup.',
                       'Neither mode supports retries.'):
        text = 'api supports retries. ' * 40 + '\n'
        boundary = len(text)
        text += constraint + '\n'
        slices(monkeypatch, [(0, boundary, 'Claim'), (0, len(text), 'Constraint')])
        data = evidence.select_highlights({'sources': [source(text)]}, 'api', 16384)
        selected = data['sources'][0]
        assert len(selected['highlights']) == 1
        assert selected['highlights'][0]['text'] == text
        assert constraint in selected['highlights'][0]['text']
        assert selected['selection_omitted_highlight_count'] == 0
        assert selected['redundant_highlight_count'] == 1
        for quote in selected['highlights']:
            assert quote['text'] == text[quote['start']:quote['end']]
        bounded = apply_budget(data, 16384)
        bounded.pop('budget')
        assert bounded == data


def test_ranking_work_is_bounded_when_coverage_and_caps_are_stable(monkeypatch):
    import builtins

    lines = [f'api operation_{i}.\n' for i in range(800)]
    text = ''.join(lines)
    intervals = []
    position = 0
    for line in lines:
        intervals.append((position, position + len(line), 'Operation'))
        position += len(line)
    slices(monkeypatch, intervals)
    ranking_work = []
    def measured_sorted(values, *, key=None, reverse=False):
        if key is not None:
            # Only candidate ranking uses a sort key in this module. Counting
            # candidates passed to it is deterministic, unlike wall-clock time.
            ranking_work.append(len(values))
        return builtins.sorted(values, key=key, reverse=reverse)
    monkeypatch.setattr(evidence, 'sorted', measured_sorted, raising=False)
    original = {'sources': [source(text)]}
    data = evidence.select_highlights(original, 'api', 16384)
    assert len(ranking_work) == 2
    assert sum(ranking_work) <= 2 * len(lines)
    assert [h['text'] for h in data['sources'][0]['highlights']] == lines[:3]
    assert data['sources'][0]['selection_omitted_highlight_count'] == len(lines) - 3
    bounded = apply_budget(data, 16384)
    bounded.pop('budget')
    assert bounded == data
    ranking_work.clear()
    assert evidence.select_highlights(original, 'api', 16384) == data
    assert len(ranking_work) == 2
    assert sum(ranking_work) <= 2 * len(lines)


def test_allowed_snippet_and_title_reductions_do_not_prevent_exact_evidence_admission():
    for field in ('provider_snippet', 'title'):
        document = source('# api\napi works.\n')
        document[field] = 'x' * 4000
        original = {'sources': [document]}
        before = deepcopy(original)
        selected = evidence.select_highlights(original, 'api', 4096)
        bounded = apply_budget(selected, 4096)
        assert original == before
        assert bounded['sources'][0]['highlights'] == selected['sources'][0]['highlights']
        assert bounded['sources'][0]['highlights'][0]['text'] == document['content']
        assert bounded['sources'][0]['truncated_fields'] == [field]
        assert len(bounded['sources'][0][field]) < 4000
        assert bounded['selection']['covered_query_terms'] == ['api']
        assert bounded['budget']['partial_coverage']
        assert wire_bytes(bounded) <= 4096


def test_metadata_only_overflow_omits_unselected_source_with_original_indices():
    original = {'sources': [source('# api\napi works.\n'),
                source('# Other\nother_identifier works.\n', 'https://other.test/' + 'x' * 3500)]}
    before = deepcopy(original)
    selected = evidence.select_highlights(original, 'api other_identifier', 4096)
    assert original == before
    assert len(selected['sources']) == 2  # Keep internal original-index mapping.
    bounded = apply_budget(selected, 4096)
    assert bounded['budget']['source_count'] == 2
    assert bounded['budget']['returned_source_count'] == 1
    assert bounded['budget']['omitted_source_indices'] == [1]
    assert bounded['budget']['partial_coverage']
    assert bounded['sources'][0]['highlights'] == selected['sources'][0]['highlights']
    assert bounded['sources'][0]['highlights'][0]['text'] == original['sources'][0]['content']
    assert bounded['selection']['covered_query_terms'] == ['api']
    assert bounded['selection']['uncovered_query_terms'] == ['other_identifier']
    assert selected['sources'][1]['selection_omitted_highlight_count'] == 1
    assert wire_bytes(bounded) <= 4096


def test_partial_overlap_unions_both_warnings_and_frees_a_slot(monkeypatch):
    left = 'Warning: alpha must not retry.\n'
    middle = 'alpha supports scheduling. ' * 30 + '\n'
    right = 'Caution: beta requires cleanup in version 2.3.\n'
    tail = 'gamma reports cancellation.\ndelta stops siblings.\n'
    text = left + middle + right + tail
    end = len(left + middle + right)
    split = end + len('gamma reports cancellation.\n')
    slices(monkeypatch, [(0, len(left + middle), 'Left'), (len(left), end, 'Right'),
                         (end, split, 'Gamma'), (split, len(text), 'Delta')])
    original = {'sources': [source(text)]}
    selected = evidence.select_highlights(original, 'alpha beta gamma delta', 8192)
    quotes = selected['sources'][0]['highlights']
    assert len(quotes) == 3
    union = next(q for q in quotes if q['start'] == 0)
    assert union['end'] == end
    assert union['text'] == left + middle + right
    assert 'must not retry' in union['text'] and 'requires cleanup' in union['text']
    assert selected['selection']['uncovered_query_terms'] == []
    assert selected['sources'][0]['selection_omitted_highlight_count'] == 0
    assert selected['sources'][0]['redundant_highlight_count'] == 1
    assert all(a['end'] <= b['start'] or b['end'] <= a['start']
               for i, a in enumerate(quotes) for b in quotes[i + 1:])
    assert apply_budget(selected, 8192)['sources'][0]['highlights'] == quotes
    assert original['sources'][0]['content'] == text
    class Cache:
        def lookup(self, url, version):
            return original['sources'][0]
    expanded = evidence.expand_evidence(Cache(), [union['evidence_id']])
    assert expanded['sources'][0]['highlights'] == [union]


def test_replacing_duplicate_owner_rebuilds_exact_range_references(monkeypatch):
    text = 'alpha works. ' * 40 + '\n'
    boundary = len(text)
    text += 'Warning: alpha must not retry.\n'
    slices(monkeypatch, [(0, boundary, 'Claim'), (0, len(text), 'Warning')])
    originals = [source(text), source(text, 'https://other.test/docs')]
    selected = evidence.select_highlights({'sources': originals}, 'alpha', 8192)
    owner, duplicate = selected['sources']
    assert len(owner['highlights']) == 1
    assert not duplicate['highlights']
    quote = owner['highlights'][0]
    reference = duplicate['duplicate_evidence'][0]
    assert quote['text'] == text
    assert reference['duplicate_of'] == quote['evidence_id']
    assert (reference['start'], reference['end']) == (0, len(text))
    class Cache:
        def lookup(self, url, version):
            return next((s for s in originals if s['requested_url'] == url and
                         s['content_version'] == version), None)
    for identifier in (quote['evidence_id'], reference['evidence_id'], reference['duplicate_of']):
        canonical, start, end = evidence.resolve_evidence(Cache(), identifier)
        assert canonical['content'][start:end] == text
    bounded = apply_budget(selected, 8192)
    assert bounded['sources'] == selected['sources']
    assert wire_bytes(bounded) <= 8192


def test_nonfitting_union_keeps_admitted_qualification_and_recounts_bytes(monkeypatch):
    from kagi_crawler import budget

    left = 'Warning: alpha must not retry.\n'
    middle = 'alpha works. ' * 20 + '\n'
    right = 'Caution: alpha requires cleanup. ' + '界\\" ' * 1800 + '\n'
    text = left + middle + right
    slices(monkeypatch, [(0, len(left + middle), 'Left'), (len(left), len(text), 'Right')])
    attempts = []
    def probe(data, limit):
        bounded = apply_budget(data, limit)
        attempts.append((deepcopy(data['sources'][0]['highlights']), bounded))
        return bounded
    monkeypatch.setattr(budget, 'apply_budget', probe)
    selected = evidence.select_highlights({'sources': [source(text)]}, 'alpha', 4096)
    quotes = selected['sources'][0]['highlights']
    assert len(quotes) == 1
    assert quotes[0]['text'] == left + middle
    union_attempts = [(qs, b) for qs, b in attempts if any(q['text'] == text for q in qs)]
    assert union_attempts
    assert all(b['sources'][0]['highlights'] != qs for qs, b in union_attempts)
    assert selected['sources'][0]['selection_omitted_highlight_count'] == 1
    bounded = apply_budget(selected, 4096)
    assert bounded['sources'][0]['highlights'] == quotes
    assert wire_bytes(bounded) <= 4096


def test_merge_at_cap_retries_disjoint_candidates_in_freed_slot(monkeypatch):
    first = 'alpha works.\n'
    second = 'alpha retries.\n'
    third = 'alpha schedules.\n'
    ballast = 'Warning: cleanup is required. ' + 'background ' * 200 + '\n'
    tail = 'alpha completes. ' + 'context ' * 50 + '\n'
    text = first + ballast + second + third + tail
    b = len(first + ballast)
    c = b + len(second)
    d = c + len(third)
    slices(monkeypatch, [(0, len(first), 'First'), (b, c, 'Second'),
                         (c, d, 'Third'), (d, len(text), 'Tail'), (0, c, 'Union')])
    selected = evidence.select_highlights({'sources': [source(text)]}, 'alpha', 16384)
    quotes = selected['sources'][0]['highlights']
    assert len(quotes) == 3
    assert any(q['text'] == first + ballast + second for q in quotes)
    assert any(q['text'] == third for q in quotes)
    assert any(q['text'] == tail for q in quotes)
    assert selected['sources'][0]['selection_omitted_highlight_count'] == 0
    assert selected['sources'][0]['redundant_highlight_count'] == 2


def test_fitting_union_is_charged_once_with_utf8_and_escaping(monkeypatch):
    left = 'Warning: alpha must not retry.\n'
    middle = 'alpha ' + '界\\" ' * 250 + '\n'
    right = 'Caution: beta requires cleanup.\n'
    text = left + middle + right
    slices(monkeypatch, [(0, len(left + middle), 'Left'), (len(left), len(text), 'Right')])
    original = {'sources': [source(text)]}
    roomy = evidence.select_highlights(original, 'alpha beta')
    limit = max(4096, wire_bytes(apply_budget(roomy, 65536)))
    selected = evidence.select_highlights(original, 'alpha beta', limit)
    quotes = selected['sources'][0]['highlights']
    assert len(quotes) == 1
    assert quotes[0]['text'] == text
    assert (quotes[0]['start'], quotes[0]['end']) == (0, len(text))
    assert quotes[0]['evidence_id'] == evidence.evidence_id(source(text), 0, len(text))
    bounded = apply_budget(selected, limit)
    assert bounded['sources'][0]['highlights'] == quotes
    assert wire_bytes(bounded) <= limit
    # Charging the shared middle twice would evict evidence at this same limit.
    doubled = deepcopy(selected)
    doubled['sources'][0]['highlights'].append(deepcopy(quotes[0]))
    assert apply_budget(doubled, limit)['sources'][0]['highlights'] != doubled['sources'][0]['highlights']


@pytest.mark.parametrize('heading,body', [
    ('Related pages', '\n'.join(f'* [tokio select cancellation safety](https://example.test/topic/{i})' for i in range(6))),
    ('Sections', '\n'.join(f'* [tokio select cancellation safety](https://example.test/topic/{i})' for i in range(6))),
    ('Related topics', '| Topic | Replies | Views |\n| --- | --- | --- |\n' + '\n'.join(
        f'| [tokio select cancellation safety](https://example.test/topic/{i}) | 2 | 123 |' for i in range(6))),
    ('Tabs', '[![](https://example.test/avatar.png)](https://example.test/user)\n' + '\n'.join(
        f'[tokio select cancellation safety](https://example.test/{"long-path/" * 20}{i})' for i in range(6))),
])
def test_link_dense_navigation_loses_to_answer_prose(heading, body):
    text = f'# {heading}\n\n{body}\n\n# Answer\n\nCancellation safety describes dropped futures.\n'
    original = {'sources': [source(text)]}
    before = deepcopy(original)
    selected = evidence.select_highlights(original, 'tokio select cancellation safety')
    quotes = selected['sources'][0]['highlights']
    assert quotes[0]['section'] == 'Answer'
    assert original == before
    # This is a penalty, not a filter: matching navigation remains eligible.
    assert any(q['section'] == heading for q in quotes)
    assert selected['selection']['uncovered_query_terms'] == []
    for quote in quotes:
        assert quote['text'] == text[quote['start']:quote['end']]
        assert quote['evidence_id'] == evidence.evidence_id(original['sources'][0], quote['start'], quote['end'])


@pytest.mark.parametrize('heading', ['API index', 'Reference list', 'Related pages'])
def test_link_lists_that_are_the_answer_remain_selectable(heading):
    text = f'# {heading}\n\n* [`Receiver::recv`](https://example.test/recv)\n* [`StreamExt::next`](https://example.test/next)\n'
    selected = evidence.select_highlights({'sources': [source(text)]}, 'Receiver::recv StreamExt::next', 4096)
    assert selected['sources'][0]['highlights'][0]['text'] == text
    assert selected['selection']['uncovered_query_terms'] == []


@pytest.mark.parametrize('code', [
    '```markdown\n' + '\n'.join('[cancellation safety](https://example.test/)' for _ in range(6)) + '\n```',
    '\n'.join('`[cancellation safety](https://example.test/)`' for _ in range(6)),
])
def test_markdown_links_in_code_are_not_navigation(code):
    text = '# Markdown example\n\n' + code + '\n\n# Other\n\nCancellation is discussed here.\n'
    selected = evidence.select_highlights({'sources': [source(text)]}, 'cancellation safety')
    assert selected['sources'][0]['highlights'][0]['section'] == 'Markdown example'


def test_answer_with_ordinary_citations_keeps_its_rank():
    text = '# Answer\n\nCancellation safety describes dropped futures and preserves application state. '
    text += 'See the [reference](https://example.test/reference).\n\n'
    text += '# Other\n\nCancellation safety ' + 'background material ' * 20 + '.\n'
    selected = evidence.select_highlights({'sources': [source(text)]}, 'cancellation safety')
    assert selected['sources'][0]['highlights'][0]['section'] == 'Answer'


@pytest.mark.parametrize('query,word', [
    ('workspaces', 'workspace'), ('packages', 'package'),
    ('classes', 'class'), ('boxes', 'box'), ('workspace', 'workspaces'),
])
def test_plural_matching_preserves_query_terms_and_literal_quotes(query, word):
    text = f'# Configuration\n\n{word} configuration.\n'
    original = {'sources': [source(text)]}
    before = deepcopy(original)
    selected = evidence.select_highlights(original, query)
    assert selected['selection']['query_terms'] == [query]
    assert selected['selection']['covered_query_terms'] == [query]
    assert selected['selection']['uncovered_query_terms'] == []
    quote = selected['sources'][0]['highlights'][0]
    assert quote['text'] == text
    assert quote['evidence_id'] == evidence.evidence_id(original['sources'][0], 0, len(text))
    assert original == before


@pytest.mark.parametrize('query', ['uv', 'workspaces', 'tool'])
def test_dotted_config_parts_match_bare_query_terms(query):
    text = '# Configuration\n\n```toml\n[tool.uv.workspace]\nmembers = ["packages/*"]\n```\n'
    selected = evidence.select_highlights({'sources': [source(text)]}, query)
    assert selected['sources'][0]['highlights'][0]['text'] == text
    assert selected['selection']['covered_query_terms'] == [query]


@pytest.mark.parametrize('query,identifier', [
    ('asyncio.TaskGroup', 'other.TaskGroup'),
    ('asyncio.TaskGroups', 'asyncio.TaskGroup'),
    ('tool.uv.workspaces', 'tool.uv.workspace'),
    ('task_flags', 'task_flag'), ('v2.4', 'v2.5'),
    ('status', 'statu'), ('class', 'clas'), ('analysis', 'analysi'),
])
def test_matching_aliases_do_not_fold_qualified_or_technical_constraints(query, identifier):
    selected = evidence.select_highlights({'sources': [source(identifier)]}, query)
    assert selected['sources'][0]['relevance'] == 'no_lexical_match'
    assert selected['selection']['uncovered_query_terms'] == evidence.tokens(query)


def test_coverage_stopwords_still_match_score_and_report():
    text = '# Prose\n\nhow to with\n'
    selected = evidence.select_highlights({'sources': [source(text)]}, 'how to with packages')
    assert selected['sources'][0]['highlights'][0]['text'] == text
    assert selected['selection']['covered_query_terms'] == ['how', 'to', 'with']
    assert selected['selection']['uncovered_query_terms'] == ['packages']


def test_rare_marginal_term_beats_several_ubiquitous_terms(monkeypatch):
    lines = [f'alpha beta gamma operation_{i}.\n' for i in range(10)]
    lines.append('delta ' + 'ballast ' * 400 + '.\n')
    text = ''.join(lines)
    intervals = []
    position = 0
    for line in lines:
        intervals.append((position, position + len(line), None))
        position += len(line)
    slices(monkeypatch, intervals)
    selected = evidence.select_highlights({'sources': [source(text)]}, 'alpha beta gamma delta')
    assert selected['sources'][0]['highlights'][0]['text'] == lines[-1]
    assert selected['selection']['uncovered_query_terms'] == []


def test_function_word_coverage_does_not_displace_configuration():
    text = '# Prose\n\nhow to with\n\n# Configuration\n\nworkspace\n'
    selected = evidence.select_highlights({'sources': [source(text)]}, 'how to with workspaces')
    assert selected['sources'][0]['highlights'][0]['section'] == 'Configuration'
    assert selected['selection']['covered_query_terms'] == ['how', 'to', 'with', 'workspaces']
