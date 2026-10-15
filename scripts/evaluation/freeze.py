"""Opt-in real HTTP corpus freezing; run with project Python and browser setup.

PYTHONPATH=src python scripts/evaluation/freeze.py --output /tmp/corpus.json
Real-page intent cases: PYTHONPATH=src python scripts/evaluation/freeze.py --real-pages
(See highlights.py for corpus/expectation fields and browser setup.)
Then deliberately replace data/corpus.json to version a new acquisition. Normal
replay/tests never refresh this file. HTML fixtures are exact preparations from
existing tests at c7f1cbca; labels in queries.json predate this acquisition.
"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
from aiohttp import web
from test_fetch import site
from test_http import running, client, body
from kagi_crawler.evidence import select_highlights
from kagi_crawler.budget import apply_budget
from kagi_crawler.safety import safe_sources

DATA = Path(__file__).parent / 'data'


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def compile_gold(case, documents):
    spans = []
    for anchor in case['anchors']:
        source = documents[anchor['document']]
        text = source['content']
        pattern = r'\s+'.join(re.escape(p) for p in anchor['anchor'].split())
        matches = list(re.finditer(pattern, text))
        if len(matches) != 1:
            raise ValueError(f"Gold must match exactly once: {case['id']} {anchor}")
        match = matches[0]
        spans.append(dict(document=anchor['document'], role=anchor['role'],
            requested_url=source['requested_url'], content_version=source['content_version'],
            start=match.start(), end=match.end(), text=match.group(),
            sha256=hashlib.sha256(match.group().encode()).hexdigest()))
    return spans


async def capture(output, cache_directory):
    cache_directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((DATA / 'fixtures.json').read_text())
    for item in manifest:
        item['html_sha256'] = hashlib.sha256((DATA / (item['id'] + '.html')).read_bytes()).hexdigest()
    queries = json.loads((DATA / 'queries.json').read_text())['cases']
    html = {m['id']: (DATA / (m['id'] + '.html')).read_text() for m in manifest}
    async def handler(request):
        return web.Response(content_type='text/html', text=html[request.path.strip('/')])
    documents = {}
    snapshots = []
    started = time.perf_counter()
    async with site(handler) as (base, options):
        async with running(cache_directory, acquisition_options=options) as (url, _), client(url, timeout=120) as session:
            for m in manifest:
                target = base + '/' + m['id']
                fetched = body(await session.call_tool('fetch', {'urls': [target],
                    'response_budget_bytes': 262144, 'freshness': 'fresh_only'}))
                source = fetched['sources'][0]
                assert 'error' not in source, source
                assert not source.get('truncated_fields'), source.get('truncated_fields')
                assert hashlib.sha256(source['content'].encode()).hexdigest() == source['content_version']
                documents[m['id']] = source
            acquisition_seconds = time.perf_counter() - started
            for case in queries:
                targets = [documents[d]['requested_url'] for d in case['documents']]
                canonical = body(await session.call_tool('fetch', {'urls': targets,
                    'freshness': 'cache_only', 'response_budget_bytes': 262144}))
                assert not canonical['budget']['partial_coverage'] or any(s['coverage']['partial'] for s in canonical['sources'])
                assert all(not s.get('truncated_fields') for s in canonical['sources'])
                canonical.pop('budget')
                gold = compile_gold(case, documents)
                for budget in (65536, 16384, 4096):
                    live = body(await session.call_tool('fetch', {'urls': targets, 'query': case['query'],
                        'freshness': 'cache_only', 'response_budget_bytes': budget}))
                    # Replay current server ordering, not the archived lexical
                    # experiment: reject unsafe sources before budgeted selection.
                    replay = deepcopy(canonical)
                    for source in replay['sources']:
                        source.pop('link_targets', None)
                        source.pop('links_truncated', None)
                    replay = safe_sources(replay, ('client-secret', 'subscriber-secret'))
                    if 'error' not in replay:
                        replay = select_highlights(replay, case['query'], budget)
                    replay = apply_budget(safe_sources(replay, ('client-secret', 'subscriber-secret')), budget)
                    assert live == replay, f"HTTP/replay mismatch: {case['id']} {budget}"
                    # Expand every ID, including duplicate attribution, through the actual cache.
                    for source in live['sources']:
                        for quote in source.get('highlights', []) + source.get('duplicate_evidence', []):
                            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']],
                                'response_budget_bytes': 262144}))['sources'][0]
                            assert expanded['content_version'] == source['content_version']
                            assert expanded['highlights'][0]['evidence_id'] == quote['evidence_id']
                    snapshots.append(dict(id=case['id'], budget=budget, production_sha256=digest(live)))
                case['gold'] = gold
            # Deliberately changed fixture acquisition; old references still resolve.
            old = body(await session.call_tool('fetch', {'urls': [documents['J']['requested_url']],
                'query': 'connect_safe', 'freshness': 'cache_only'}))['sources'][0]
            quote = old['highlights'][0]
            html['J'] = html['J'].replace('2.4', '2.5')
            new = body(await session.call_tool('fetch', {'urls': [documents['J']['requested_url']],
                'freshness': 'refresh'}))['sources'][0]
            assert new['content_version'] != old['content_version']
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert expanded['highlights'] == [quote]
        async with running(cache_directory, acquisition_options=options) as (url, _), client(url) as session:
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert expanded['highlights'] == [quote]
        async with running(cache_directory, acquisition_options=options, cache_cap_bytes=1024) as (url, _), client(url) as session:
            expanded = body(await session.call_tool('fetch', {'evidence_ids': [quote['evidence_id']]}))['sources'][0]
            assert expanded['error']['code'] == 'evidence_unavailable'
    # A capture may include uncommitted production changes. Do not attribute
    # its snapshots to the fixture preparation revision or invent an exact commit.
    provenance = dict(scope='current production working tree at capture; not the archived lexical baseline',
        source_sha256={name: hashlib.sha256((ROOT / 'src/kagi_crawler' / name).read_bytes()).hexdigest()
            for name in ('server.py', 'evidence.py', 'budget.py', 'safety.py')})
    corpus = dict(schema_version=1, baseline_revision='current-working-tree',
        baseline_provenance=provenance, fixture_manifest=manifest,
        documents=documents, cases=queries, acquisition_limits=canonical['acquisition_limits'],
        production_snapshots=snapshots, acquisition_seconds=acquisition_seconds,
        contracts={'http_replay_parity': True, 'all_ids_expanded': True,
                   'refresh_retained_version': True, 'restart_retained_version': True,
                   'eviction_unavailable': True})
    output.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + '\n')
    return corpus


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--real-pages':
        # Real acquisitions bypass the synthetic HTTP fixture server entirely.
        from highlights import main as highlight_main
        sys.argv[1] = 'freeze'
        highlight_main()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--output', type=Path, required=True)
        args = parser.parse_args()
        with tempfile.TemporaryDirectory(prefix='ncn-freeze-') as temp:
            asyncio.run(capture(args.output, Path(temp)))
