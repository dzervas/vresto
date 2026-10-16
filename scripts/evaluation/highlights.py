"""Real-page highlight evaluation, alongside the archived synthetic experiment.

Freeze (network/browser, opt-in):
  uv run --locked python scripts/evaluation/run.py highlights freeze
Score (offline):
  uv run --locked python scripts/evaluation/run.py highlights score --save-baseline eval/corpus/baseline.json
  uv run --locked python scripts/evaluation/run.py highlights score --compare eval/corpus/baseline.json
Browser setup is the application's normal VRESTO_HEADFUL/channel environment.
Run headed capture under a private Xvfb; this script owns/closes Acquisition only.

highlight_cases.json is {schema_version: 1, cases: [...]}. Each case has id,
query (original query, or an intent-derived probe for historical full-text calls),
sources (ordered corpus document IDs), expected (literal answer strings, each
required in at least one whole highlight), response_budget_bytes (original shared
budget), expectation_kind (answer, absent, unavailable, or discovery_only), and
note (label rationale/availability). Empty expected means absent answer or
unavailable/unlabelled evidence, not successful recall: recall is null.

corpus.json contains documents (ID -> canonical Acquisition source, including
fetched_at/content_version), cases (source IDs and original args), discovery
(per-case ordered url/title/provider_snippet records, never acquired evidence),
and frozen_at. Failed acquisitions remain explicit error records. Freeze resumes
existing successful records unless --refresh; it never reconstructs canonical
text from old highlights. Scoring verifies versions and all literal labels before
running the current production selector and shared wire-budget gate. Cases marked
memory_paraphrase belong to the global memory metric (run.py memory), not this
historical per-search-source baseline, and are excluded here. UTF-8 byte
metrics count emitted highlights, not JSON overhead; overlap is summed quote
bytes minus interval-union bytes within each source. Absent cases' exposure is
reported separately. Comparisons reject changed corpus or labels.
"""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from vresto.budget import apply_budget
from vresto.evidence import select_highlights

ROOT = Path(__file__).resolve().parents[2]


def load(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def recorded_result(record):
    result = record['result']
    if 'structuredContent' in result:
        return result['structuredContent']
    return json.loads(next(item['text'] for item in result['content'] if item['type'] == 'text'))


async def freeze(args):
    # Lazy import keeps scoring independent of browser startup and test helpers.
    from vresto.acquisition import Acquisition

    corpus = load(args.corpus) if args.corpus.exists() and not args.refresh else dict(
        schema_version=1, documents={}, cases=[], discovery={})
    documents = corpus['documents']
    corpus['cases'] = []
    acquisition = Acquisition()
    try:
        for case in load(args.inputs):
            cid, original = case['case_id'], case['new']['args']
            discovery = []
            if 'urls' in original:
                urls = original['urls']
            else:
                recorded = recorded_result(load(args.recordings / f'{cid}.new.json'))
                discovery = [{k: row.get(k, '') for k in ('url', 'title', 'provider_snippet')}
                             for row in recorded.get('results', [])]
                urls = [row['url'] for row in discovery]
            corpus['discovery'][cid] = discovery
            ids = []
            for url in urls:
                sid = hashlib.sha256(url.encode()).hexdigest()[:16]
                ids.append(sid)
                if sid not in documents or 'error' in documents[sid]:
                    source = (await acquisition.fetch_many([url]))['sources'][0]
                    documents[sid] = source
                    print(cid, url, source.get('error', len(source.get('content', ''))), flush=True)
                    save(args.corpus, corpus)
            corpus['cases'].append(dict(id=cid, sources=ids, args=original))
            corpus['frozen_at'] = datetime.now(timezone.utc).isoformat()
            save(args.corpus, corpus)
    finally:
        await acquisition.close()


def union_bytes(text, intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum(len(text[a:b].encode()) for a, b in merged)


def score(corpus, labels):
    # Keep the original corpus/expectation digest strict and comparable; global
    # memory paraphrases do not invent new recorded discovery/acquisition calls.
    labels = {**labels, 'cases': [case for case in labels['cases'] if not case.get('memory_paraphrase')]}
    documents = corpus['documents']
    for sid, source in documents.items():
        if 'error' not in source:
            version = hashlib.sha256(source['content'].encode()).hexdigest()
            if source['content_version'] != version:
                raise ValueError(f'Canonical version mismatch: {sid}')
    rows = []
    frozen_cases = {case['id']: case for case in corpus['cases']}
    if set(frozen_cases) != {case['id'] for case in labels['cases']}:
        raise ValueError('Labels and corpus must contain the same cases')
    for case in labels['cases']:
        if case['sources'] != frozen_cases[case['id']]['sources']:
            raise ValueError(f"Source list mismatch: {case['id']}")
        sources = [deepcopy(documents[sid]) for sid in case['sources']]
        expected = case['expected']
        if case.get('expectation_kind') == 'absent' and any(
                case['query'] in s.get('content', '') for s in sources):
            raise ValueError(f"Absent-answer probe exists in canonical text: {case['id']}")
        for literal in expected:
            if not literal or not any(literal in s.get('content', '') for s in sources):
                raise ValueError(f"Expected string absent from canonical text: {case['id']} {literal!r}")
        discovery = corpus.get('discovery', {}).get(case['id'], [])
        key = 'results' if discovery else 'sources'
        if discovery:
            if len(discovery) != len(sources):
                raise ValueError(f"Discovery/source count mismatch: {case['id']}")
            sources = [{**row, **source} for row, source in zip(discovery, sources)]
        # Mirror Retrieval._finalize: private traversal metadata never reaches
        # selection or the wire budget in production.
        for source in sources:
            source.pop('link_targets', None)
            source.pop('links_truncated', None)
        data = {key: sources, 'content_mode': 'full_text'}
        selected = apply_budget(select_highlights(data, case['query'], case['response_budget_bytes']),
                                case['response_budget_bytes'])
        highlights = [q for s in selected[key] for q in s.get('highlights', [])]
        total = sum(len(q['text'].encode()) for q in highlights)
        bearing = sum(len(q['text'].encode()) for q in highlights
                      if any(literal in q['text'] for literal in expected))
        found = sum(any(literal in q['text'] for q in highlights) for literal in expected)
        overlap = 0
        originals = {s['requested_url']: s for s in sources}
        for source in selected[key]:
            quotes = source.get('highlights', [])
            text = originals[source['requested_url']].get('content', '')
            overlap += sum(len(q['text'].encode()) for q in quotes) - union_bytes(
                text, [(q['start'], q['end']) for q in quotes])
        rows.append(dict(id=case['id'], expectation_kind=case.get('expectation_kind', 'answer'),
                         missing_expected=[literal for literal in expected
                                           if not any(literal in q['text'] for q in highlights)],
                         recall=found / len(expected) if expected else None,
                         expected_count=len(expected), found_count=found,
                         highlight_bytes=total, answer_bearing_share=bearing / total if total else 0,
                         answer_bearing_bytes=bearing, overlap_bytes=overlap,
                         highlight_count=len(highlights), first_section=highlights[0]['section'] if highlights else None,
                         failed_sources=sum('error' in s for s in sources)))
    answerable = [r for r in rows if r['expected_count']]
    total = sum(r['highlight_bytes'] for r in rows)
    summary = dict(cases=len(rows), labelled_answer_cases=len(answerable),
                   macro_recall=sum(r['recall'] for r in answerable) / len(answerable) if answerable else None,
                   micro_recall=sum(r['found_count'] for r in rows) / sum(r['expected_count'] for r in rows)
                   if answerable else None,
                   complete_cases=sum(r['recall'] == 1 for r in answerable), highlight_bytes=total,
                   answer_bearing_share=sum(r['answer_bearing_bytes'] for r in rows) / total if total else 0,
                   overlap_bytes=sum(r['overlap_bytes'] for r in rows),
                   highlight_count=sum(r['highlight_count'] for r in rows),
                   empty_expectation_exposure_bytes=sum(r['highlight_bytes'] for r in rows if not r['expected_count']),
                   absent_answer_exposure_bytes=sum(r['highlight_bytes'] for r in rows
                                                    if r['expectation_kind'] == 'absent'))
    return dict(schema_version=1, corpus_sha256=digest(corpus), labels_sha256=digest(labels),
                selector_sha256={name: hashlib.sha256((ROOT / 'src' / name).read_bytes()).hexdigest()
                                 for name in ('evidence.py', 'budget.py')},
                cases=rows, aggregates=summary)


def compare(report, baseline):
    if any(report[k] != baseline[k] for k in ('corpus_sha256', 'labels_sha256')):
        raise ValueError('Cannot compare different corpus/expectations')
    previous = {r['id']: r for r in baseline['cases']}
    for row in report['cases']:
        old = previous[row['id']]
        row['delta'] = {k: row[k] - old[k] for k in (
            'recall', 'highlight_bytes', 'answer_bearing_share', 'overlap_bytes', 'highlight_count')
            if row[k] is not None and old[k] is not None}
        row['first_section_changed'] = row['first_section'] != old['first_section']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'score'))
    parser.add_argument('--corpus', type=Path, default=ROOT / 'eval/corpus/corpus.json')
    parser.add_argument('--cases', type=Path, default=ROOT / 'eval/highlight_cases.json')
    parser.add_argument('--inputs', type=Path, default=ROOT / 'eval/exa/cases.json')
    parser.add_argument('--recordings', type=Path, default=Path('/tmp/kc-eval'))
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--save-baseline', type=Path)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    if args.command == 'freeze':
        asyncio.run(freeze(args))
    else:
        report = score(load(args.corpus), load(args.cases))
        if args.compare:
            compare(report, load(args.compare))
        if args.save_baseline:
            save(args.save_baseline, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
