"""Optional measured rescore-only experiment, never a production reranker.

Model-free: PYTHONPATH=src python scripts/evaluation/run.py --output /tmp/lexical.json
CPU paired: add --worker-python /tmp/kagi-ncn-worker/bin/python
  --model-dir /tmp/kagi-ncn-model --library-path "$(cat /tmp/kn8-library-path)".
Install ONLY isolated worker: uv venv /tmp/kagi-ncn-worker; uv pip sync --python
/tmp/kagi-ncn-worker/bin/python scripts/evaluation/requirements.lock --require-hashes.
Acquire public artifacts explicitly with acquire_model.py. No automatic download.

Frozen 16-case synthetic workload is deliberately not a representative benchmark.
All positive production BM25 candidates are paired, no top-K restriction. Full
canonical windows are diagnosed separately. Same 3/source cap, dedup, metadata,
wire budget, and exact-byte evidence contracts. No scores leak into evidence.
The isolated reranked wire envelope deliberately retains identical lexical-pool
selection metadata for budget control; the sidecar arm/model fields disclose its
changed ordering. This is not a production HTTP reranker response.
Model logits are relevance ordering, not support or abstention probabilities.
Overflow policy is predeclared: any total pair >512 => whole-query lexical
fallback, including warnings/scopes. No truncation or prefix scoring permitted.

The checked data/results.json records the actual experiment, not a required test
expectation of model benefit. Recommendation is defer production adoption unless
measured qualification-complete coverage gains warrant a separate user review.
"""
import argparse
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import select
import signal
import subprocess
import tempfile
import time

from kagi_crawler.budget import apply_budget, wire_bytes
from kagi_crawler.evidence import evidence_id, select_highlights
from kagi_crawler.safety import safe_sources
from selection import candidates, rescore_order, select_order

DATA = Path(__file__).parent / 'data'


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def intervals_union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def measure(case, data, documents):
    quotes = [(s, q) for s in data['sources'] for q in s.get('highlights', [])]
    by_url = defaultdict(list)
    unique = defaultdict(list)
    versions = {s['content_version']: s['content'] for s in documents.values()}
    for source, quote in quotes:
        canonical = next(s for s in documents.values() if s['requested_url'] == source['requested_url'])
        assert quote['text'] == canonical['content'][quote['start']:quote['end']]
        assert source['content_version'] == hashlib.sha256(canonical['content'].encode()).hexdigest()
        assert source['resolved_url'] == canonical['resolved_url'] and quote['url'] == canonical['resolved_url']
        assert quote['evidence_id'] == evidence_id(canonical, quote['start'], quote['end'])
        by_url[source['requested_url']].append((quote['start'], quote['end']))
        unique[source['content_version']].append((quote['start'], quote['end']))
    for source in data['sources']:
        for ref in source.get('duplicate_evidence', []):
            canonical = next(s for s in documents.values() if s['requested_url'] == source['requested_url'])
            assert ref['evidence_id'] == evidence_id(canonical, ref['start'], ref['end'])
            assert ref['url'] == canonical['resolved_url']
            originals = [s for s in documents.values() if s['content_version'] == canonical['content_version']
                         and s['requested_url'] != canonical['requested_url']]
            assert any(ref['duplicate_of'] == evidence_id(s, ref['start'], ref['end']) for s in originals)
    attributed_urls = {s['requested_url'] for s in data['sources'] if s.get('highlights') or s.get('duplicate_evidence')}
    attribution_complete = all(documents[d]['requested_url'] in attributed_urls
                               for d in case.get('required_attributed_documents', []))
    gold = case['gold']
    coverage = []
    for span in gold:
        ranges = intervals_union(by_url[span['requested_url']])
        covered = sum(max(0, min(b, span['end']) - max(a, span['start'])) for a, b in ranges)
        coverage.append(covered / (span['end']-span['start']))
    claim_spans = [g for g in gold if g['role'] == 'claim']
    unsafe = 0
    qualified_claims = set()
    relevant_quotes = 0
    for source, quote in quotes:
        local = [g for g in gold if g['requested_url'] == source['requested_url']]
        claims = [g for g in local if g['role'] == 'claim' and quote['start'] <= g['start'] and quote['end'] >= g['end']]
        relevant_quotes += bool(claims)
        if claims:
            required = [g for g in local if g['role'] != 'claim']
            safe = all(quote['start'] <= g['start'] and quote['end'] >= g['end'] for g in required)
            unsafe += not safe
            if safe:
                qualified_claims.update((g['requested_url'], g['start'], g['end']) for g in claims)
    quote_bytes = sum(len(versions[v][a:b].encode()) for v, ranges in unique.items()
                      for a, b in intervals_union(ranges))
    gold_unique = defaultdict(list)
    for g in gold:
        for a, b in intervals_union(by_url[g['requested_url']]):
            start, end = max(a, g['start']), min(b, g['end'])
            if end > start:
                gold_unique[g['content_version']].append((start, end))
    useful = sum(len(versions[v][a:b].encode()) for v, ranges in gold_unique.items()
                 for a, b in intervals_union(ranges))
    return dict(verbatim_version_bound=True, gold_span_coverage=sum(coverage)/len(coverage) if coverage else None,
        all_required_spans=all(c == 1 for c in coverage) and attribution_complete if gold else None,
        attribution_complete=attribution_complete,
        qualification_complete_claim_recall=len(qualified_claims)/len(claim_spans) if claim_spans else None,
        unsafe_claim_quotes=unsafe, relevant_quote_precision=relevant_quotes/len(quotes) if quotes and gold else None,
        quote_count=len(quotes), quote_bytes=quote_bytes, gold_quote_bytes=useful,
        gold_byte_fraction=useful/quote_bytes if quote_bytes and gold else None,
        attributed_source_count=sum(bool(s.get('highlights') or s.get('duplicate_evidence')) for s in data['sources']),
        duplicate_reference_count=sum(len(s.get('duplicate_evidence', [])) for s in data['sources']),
        unsupported_exposure_bytes=quote_bytes if not case['answerable'] else None,
        unsupported_exposure_quotes=len(quotes) if not case['answerable'] else None)


def pool_output(canonical, pool):
    data = deepcopy(canonical)
    for s in data['sources']:
        s['highlights'] = []
    for _, i, a, b, section in pool:
        s = data['sources'][i]
        s['highlights'].append(dict(url=s['resolved_url'], start=a, end=b, section=section, text=s['content'][a:b],
                                   evidence_id=evidence_id(s, a, b)))
    return data


def finalize(data, budget):
    for source in data['sources']:
        source.pop('link_targets', None)
        source.pop('links_truncated', None)
    return apply_budget(safe_sources(data, ('client-secret', 'subscriber-secret')), budget)


class Worker:
    """Single outstanding request; cleanup also runs on KeyboardInterrupt/cancel.

    A sampled 512 MiB RSS ceiling supplements hard 2 GiB address-space/120 CPU-s
    limits. Polling (10 ms) and ru_maxrss are reported separately; neither claims
    system-wide browser RAM. No arbitrary inherited env or service configuration;
    verbatim canonical public passage text can contain public links, not credentials.
    """
    def __init__(self, python, model, libraries, deadline=30):
        self.temp = tempfile.TemporaryDirectory(prefix='ncn-offline-')
        self.deadline = deadline
        self.peak_rss_kib = 0
        environment = dict(HOME=self.temp.name, TMPDIR=self.temp.name, LANG='C.UTF-8',
            LD_LIBRARY_PATH=libraries, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
            MKL_NUM_THREADS='1', TOKENIZERS_PARALLELISM='false', HF_HUB_OFFLINE='1')
        self.process = subprocess.Popen([str(Path(python).absolute()), '-I',
            str(Path(__file__).with_name('worker.py')), str(Path(model).absolute())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, cwd=self.temp.name, start_new_session=True, close_fds=True)
        try:
            start = time.perf_counter()
            self.ready = self.receive()
            self.ready['parent_startup_ms'] = (time.perf_counter()-start)*1000
        except BaseException:
            self.close()
            raise

    def receive(self, end=None):
        end = end if end is not None else time.monotonic() + self.deadline
        buffer = bytearray()
        while time.monotonic() < end:
            status = Path(f'/proc/{self.process.pid}/status')
            if status.exists():
                text = status.read_text()
                if 'VmRSS:' in text:
                    rss = int(text.split('VmRSS:')[1].split()[0])
                    self.peak_rss_kib = max(self.peak_rss_kib, rss)
                    if rss > 512*1024:
                        raise RuntimeError('Worker RSS ceiling exceeded')
            ready, _, _ = select.select([self.process.stdout], [], [], 0.01)
            if ready:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError(f'Worker exited: {self.process.poll()}')
                buffer.extend(chunk)
                if len(buffer) > 65536:
                    raise RuntimeError('Worker output ceiling exceeded')
                if buffer.endswith(b'\n'):
                    return json.loads(buffer)
        raise TimeoutError('Worker deadline exceeded')

    def score(self, query, passages, **diagnostics):
        request = json.dumps(dict(query=query, passages=passages, **diagnostics), ensure_ascii=False).encode() + b'\n'
        if len(passages) > 128 or len(request) > 1024**2:
            return dict(fallback='candidate_or_request_limit', overflow=False, scores=[], lengths=[], eligible=[],
                roundtrip_ms=0.0, tokenization_ms=0.0, inference_ms=0.0,
                query_encoding_ms=0.0, passage_encoding_miss_ms=0.0, passage_reuse_hit_ms=0.0,
                passage_hits=0, passage_misses=0, excluded_overflow=[], unavailable=[])
        start = time.perf_counter()
        end = time.monotonic() + self.deadline
        try:
            # Pipe writes themselves can block if a runtime stops consuming.
            os.set_blocking(self.process.stdin.fileno(), False)
            position = 0
            while position < len(request):
                if time.monotonic() >= end:
                    raise TimeoutError('Worker deadline exceeded while writing')
                _, writable, _ = select.select([], [self.process.stdin], [], 0.01)
                if writable:
                    position += os.write(self.process.stdin.fileno(), request[position:position+4096])
            response = self.receive(end)
        except BaseException:
            self.close()
            raise
        response['roundtrip_ms'] = (time.perf_counter()-start)*1000
        if not response['fallback']:
            assert len(response['scores']) == len(response.get('eligible', passages))
            assert all(math.isfinite(s) for s in response['scores'])
        return response

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout):
            pipe.close()
        self.temp.cleanup()


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered)*fraction)-1)] if ordered else None


def load_corpus(path):
    corpus = json.loads(path.read_text())
    documents = corpus['documents']
    cases = corpus['cases']
    for source in documents.values():
        if hashlib.sha256(source['content'].encode()).hexdigest() != source['content_version']:
            raise ValueError('Invalid frozen canonical version')
    for case in cases:
        for span in case['gold']:
            source = documents[span['document']]
            if (span['requested_url'] != source['requested_url'] or
                span['content_version'] != source['content_version'] or
                not 0 <= span['start'] < span['end'] <= len(source['content']) or
                span['text'] != source['content'][span['start']:span['end']] or
                span['sha256'] != hashlib.sha256(span['text'].encode()).hexdigest()):
                raise ValueError('Invalid frozen gold: version, offset or exact quote mismatch')
    return corpus


def main():
    def cancelled(signum, frame):
        raise KeyboardInterrupt('Evaluation cancelled')
    signal.signal(signal.SIGTERM, cancelled)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=DATA / 'corpus.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker-python')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--library-path', default='')
    parser.add_argument('--worker-deadline', type=float, default=30)
    args = parser.parse_args()
    if not 0.1 <= args.worker_deadline <= 30:
        parser.error('--worker-deadline must be between 0.1 and 30 seconds')
    if bool(args.worker_python) != bool(args.model_dir):
        parser.error('--worker-python and --model-dir must be supplied together')
    corpus = load_corpus(args.corpus)
    documents, cases = corpus['documents'], corpus['cases']
    pools = {}
    for case in cases:
        canonical = dict(sources=[dict(deepcopy(documents[d]), cache_state='hit', stale=False)
            for d in case['documents']], content_mode='full_text', acquisition_limits=corpus['acquisition_limits'])
        ranked, all_windows = candidates(canonical, case['query'])
        pools[case['id']] = canonical, ranked, all_windows
    models, costs, cold = {}, [], []
    model = None
    if args.worker_python:
        manifest = json.loads((args.model_dir / 'manifest.json').read_text())
        assert manifest == json.loads((DATA / 'model.json').read_text()), 'Unpinned artifact manifest'
        # Three cold process startups (OS artifact page cache is NOT flushed).
        for run in range(3):
            worker = Worker(args.worker_python, args.model_dir, args.library_path, args.worker_deadline)
            try:
                canonical, pool, _ = pools['04']
                first = worker.score(cases[3]['query'], [canonical['sources'][i]['content'][a:b] for _, i, a, b, _ in pool])
                cold.append(dict(run=run, ready=worker.ready, first_inference_case='04',
                    first_inference_scope='one 51-token qualified-identifier candidate, batch 1; not representative workload latency',
                    first_inference=first))
                if run == 2:
                    for repetition in range(10):
                        for case in cases:
                            canonical, pool, _ = pools[case['id']]
                            started = time.perf_counter()
                            replay_pool, _ = candidates(canonical, case['query'])
                            candidate_ms = (time.perf_counter()-started)*1000
                            assert replay_pool == pool
                            response = worker.score(case['query'], [canonical['sources'][i]['content'][a:b] for _, i, a, b, _ in pool])
                            models[case['id']] = response
                            # Frozen end-to-end replay latency excludes acquisition and gold scoring.
                            started = time.perf_counter()
                            lexical = select_highlights(deepcopy(canonical), case['query'])
                            lexical_selection_ms = (time.perf_counter()-started)*1000
                            started = time.perf_counter()
                            reranked = select_order(deepcopy(canonical), rescore_order(pool, response))
                            model_selection_ms = (time.perf_counter()-started)*1000
                            pipeline = []
                            for budget in (65536, 16384, 4096):
                                started = time.perf_counter()
                                finalize(deepcopy(lexical), budget)
                                lexical_finalize_ms = (time.perf_counter()-started)*1000
                                started = time.perf_counter()
                                finalize(deepcopy(reranked), budget)
                                model_finalize_ms = (time.perf_counter()-started)*1000
                                pipeline.append(dict(budget=budget,
                                    lexical_request_ms=lexical_selection_ms+lexical_finalize_ms,
                                    reranker_request_ms=candidate_ms+response['roundtrip_ms']+model_selection_ms+model_finalize_ms,
                                    lexical_finalization_ms=lexical_finalize_ms, reranker_finalization_ms=model_finalize_ms))
                            costs.append(dict(id=case['id'], repetition=repetition, candidate_ms=candidate_ms,
                                lexical_selection_ms=lexical_selection_ms, reranker_selection_ms=model_selection_ms,
                                frozen_pipeline=pipeline, **response))
                    peak = worker.peak_rss_kib
            finally:
                worker.close()
        model = dict(manifest=manifest, cold=cold, warm=costs, sampled_peak_rss_kib=peak,
            rss_sample_interval_ms=10, memory_limit_bytes=2*1024**3, rss_limit_bytes=512*1024**2,
            deadline_seconds=args.worker_deadline, candidate_limit=128, request_bytes=1024**2, response_bytes=65536,
            cpu_seconds_limit=120, concurrency=1, token_limit_total_pair=512,
            truncation=False, cold_page_cache='not_flushed', all_workers_reaped=True,
            installed_site_packages_bytes=None,
            footprint_scope='site-packages only, including generated pyc; excludes interpreter and Nix native shared libraries')
        # Venv Python is a symlink; inventory its actual site packages, not /nix Python.
        model['installed_site_packages_bytes'] = sum(p.stat().st_size for p in Path(args.worker_python).absolute().parent.parent.glob('lib/python*/site-packages/**/*') if p.is_file())
    results, artifacts = [], []
    snapshots = {(s['id'], s['budget']): s['production_sha256'] for s in corpus['production_snapshots']}
    for case in cases:
        canonical, pool, all_windows = pools[case['id']]
        candidate_diagnostic = measure(case, pool_output(canonical, pool), documents)
        all_diagnostic = measure(case, pool_output(canonical, all_windows), documents)
        lexical = select_highlights(deepcopy(canonical), case['query'])
        controlled = select_order(deepcopy(canonical), pool)
        assert lexical == controlled, 'Prototype must equal the unmodified production baseline'
        response = models.get(case['id'])
        arms = [('lexical', controlled)]
        if response:
            arms.append(('reranker', select_order(deepcopy(canonical), rescore_order(pool, response))))
        artifacts.append(dict(id=case['id'], all_windows=[dict(source=i,start=a,end=b,section=s) for _,i,a,b,s in all_windows],
            positive_candidates=[dict(lexical_score=sc,source=i,start=a,end=b,section=s) for sc,i,a,b,s in pool],
            model=response))
        for arm, selected in arms:
            pre = measure(case, selected, documents)
            for budget in (65536, 16384, 4096):
                began = time.perf_counter()
                final = finalize(deepcopy(selected), budget)
                finalization_ms = (time.perf_counter()-began)*1000
                if arm == 'lexical':
                    assert digest(final) == snapshots[case['id'], budget], 'Frozen HTTP production output drifted'
                measured = measure(case, final, documents)
                results.append(dict(id=case['id'], query=case['query'], tags=case['tags'], answerable=case['answerable'],
                    arm=arm, budget=budget, wire_bytes=wire_bytes(final), candidate_count=len(pool),
                    all_window_count=len(all_windows), candidate_coverage=candidate_diagnostic['gold_span_coverage'],
                    all_window_coverage=all_diagnostic['gold_span_coverage'],
                    candidate_qualified_recall=candidate_diagnostic['qualification_complete_claim_recall'],
                    pre_budget_coverage=pre['gold_span_coverage'], pre_budget_success=pre['all_required_spans'],
                    fallback=response['fallback'] if arm == 'reranker' else None,
                    finalization_ms=finalization_ms, partial_coverage=final['budget']['partial_coverage'],
                    evidence=final,
                    failure_classes=dict(
                        acquisition_or_segmentation=bool(case['answerable'] and all_diagnostic['gold_span_coverage'] < 1),
                        lexical_candidate_recall=bool(case['answerable'] and candidate_diagnostic['gold_span_coverage'] < all_diagnostic['gold_span_coverage']),
                        ranking_or_three_source_cap=bool(case['answerable'] and candidate_diagnostic['gold_span_coverage'] == 1 and not pre['all_required_spans']),
                        atomic_budget=bool(case['answerable'] and pre['all_required_spans'] and not measured['all_required_spans']),
                        context_fallback=bool(arm == 'reranker' and response['fallback']),
                        absent_unsupported_exposure=bool(not case['answerable'] and measured['quote_count'])),
                    model_scored=bool(arm == 'reranker' and pool and not response['fallback']), **measured))
    summary = []
    for arm in ('lexical', 'reranker'):
        for budget in (65536, 16384, 4096):
            subset = [c for c in results if c['arm'] == arm and c['budget'] == budget and c['answerable']]
            if subset:
                summary.append(dict(arm=arm, budget=budget, answerable_cases=len(subset),
                    macro_coverage=sum(c['gold_span_coverage'] for c in subset)/len(subset),
                    all_required_successes=sum(c['all_required_spans'] for c in subset),
                    macro_qualified_recall=sum(c['qualification_complete_claim_recall'] for c in subset)/len(subset),
                    unsafe_claim_quotes=sum(c['unsafe_claim_quotes'] for c in subset),
                    quote_bytes=sum(c['quote_bytes'] for c in subset)))
    output = dict(schema_version=1, corpus_sha256=hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        baseline_revision=corpus['baseline_revision'], contracts=corpus['contracts'], model=model,
        runner_hashes={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('run.py', 'worker.py', 'selection.py', 'freeze.py', 'acquire_model.py')},
        host=dict(platform=platform.platform(), python=platform.python_version(), cpu_affinity=sorted(os.sched_getaffinity(0))),
        acquisition_seconds=corpus['acquisition_seconds'], summary=summary, cases=results,
        bounded_representation=artifacts,
        recommendation='defer_production_adoption' if model else 'model_free_baseline_only',
        recommendation_basis='Synthetic 16-case pilot; scores do not verify answers. Separate user approval required for production adoption.',
        remaining_candidate_misses=[c['id'] for c in results if c['arm'] == 'lexical' and c['budget'] == 65536
            and c['failure_classes']['lexical_candidate_recall']])
    if model:
        for budget in (65536, 16384, 4096):
            scored = {c['id'] for c in results if c['arm'] == 'reranker' and c['budget'] == budget
                      and c['answerable'] and c['model_scored']}
            for arm in ('lexical', 'reranker'):
                subset = [c for c in results if c['arm'] == arm and c['budget'] == budget and c['id'] in scored]
                summary.append(dict(arm=arm, budget=budget, slice='model_scored_answerable_only',
                    answerable_cases=len(subset), all_required_successes=sum(c['all_required_spans'] for c in subset),
                    macro_coverage=sum(c['gold_span_coverage'] for c in subset)/len(subset) if subset else None))
        pairs = {(c['id'], c['budget'], c['arm']): c for c in results if c['answerable']}
        output['paired_success_gains'] = sum(
            int(pairs[c['id'], c['budget'], 'reranker']['all_required_spans'])-int(c['all_required_spans'])
            for c in results if c['answerable'] and c['arm'] == 'lexical')
        output['paired_success_improvements'] = sum(
            bool(pairs[c['id'], c['budget'], 'reranker']['all_required_spans']) and not c['all_required_spans']
            for c in results if c['answerable'] and c['arm'] == 'lexical')
        output['paired_success_regressions'] = sum(
            not pairs[c['id'], c['budget'], 'reranker']['all_required_spans'] and bool(c['all_required_spans'])
            for c in results if c['answerable'] and c['arm'] == 'lexical')
        output['recommendation_basis'] += (f" Observed {output['paired_success_improvements']} improved and "
            f"{output['paired_success_regressions']} regressed case-budget pairs in this pilot only. "
            'Case 02 is an intentional ranking discriminator, not a general model benchmark. '
            'Candidate misses, context fallback and atomic budget loss remain distinct from ranking.')
    if costs:
        for field in ('tokenization_ms', 'inference_ms', 'roundtrip_ms'):
            vals = [c[field] for c in costs if not c['fallback'] and c.get('lengths')]
            model[field] = dict(p50=percentile(vals, .5), p95=percentile(vals, .95), n=len(vals))
        model['frozen_request_latency'] = []
        for budget in (65536, 16384, 4096):
            for arm in ('lexical', 'reranker'):
                vals = [p[arm+'_request_ms'] for c in costs for p in c['frozen_pipeline'] if p['budget'] == budget]
                model['frozen_request_latency'].append(dict(arm=arm, budget=budget, p50=percentile(vals, .5),
                    p95=percentile(vals, .95), n=len(vals), acquisition_included=False))
        model['artifact_payload_bytes'] = sum(f['bytes'] for f in model['manifest']['files'])
        model['graph_inspection'] = json.loads((DATA / 'model-graph.json').read_text())
        model['requirements_lock_sha256'] = hashlib.sha256(Path(__file__).with_name('requirements.lock').read_bytes()).hexdigest()
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
