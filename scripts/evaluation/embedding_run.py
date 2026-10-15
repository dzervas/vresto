"""Optional English HTML dense/hybrid pilot; never generates answers.

Run: PYTHONPATH=src python scripts/evaluation/embedding_run.py --output /tmp/anl.json
  --worker-python /tmp/kagi-ncn-worker/bin/python --model-dir /tmp/kagi-anl-model
  --library-path "$(cat /tmp/kn8-library-path)"
Acquire explicitly: python scripts/evaluation/acquire_model.py /tmp/kagi-anl-model --embedding
The unchanged requirements.lock pins the reused isolated CPU runtime. No default
model downloads, project dependency changes, production cache hooks or persistence.

Predeclared before inference: ALL coherent canonical windows, including zero
BM25, up to 128/request; whole-query lexical fallback above that/request byte
limit or 256 total query tokens; overflowing passages excluded atomically, not
truncated. Dense cosine order and equal-weight RRF(60+rank) over eligible dense
and positive BM25 candidates; no support threshold or tuned labels. Three/source
selection and original wire budgets/finalizer. Experimental envelope declares
semantic-candidate (NOT verified support); exact metadata overhead is measured.
Frozen ncn corpus, gold and results are inputs only. Baseline latency is frozen,
not contemporaneously measured reranker latency; the historical lexical selector
(selection.py candidates/select_order, NOT current production) is timed paired
here. Frozen baseline evidence digests remain hard checks, never refreshed.
Three cold processes (OS page cache not flushed), ten warm repetitions/case for
both explicit no-reuse and reuse conditions, plus query and passage component
costs. Acquisition excluded; previous measured acquisition retained separately.
Two separately predeclared short context-eligible paraphrase probes reuse frozen
04/14 exact gold; they do NOT relabel/replace any original case or baseline.
Their small pools test candidate inclusion, not hard ranking/general benefit.
Synthetic English pilot cannot establish multilingual support/general quality.
Small transient content volume/reuse does not by itself justify vector storage;
production adoption always needs separate user approval.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import time

from historical import wire_bytes
from run import (DATA, Worker, digest, finalize, load_corpus, measure, pool_output,
                 percentile)
from selection import candidates, embedding_order, select_order


def identities(canonical, windows):
    return [dict(version=canonical['sources'][i]['content_version'], start=a, end=b,
        text_sha256=hashlib.sha256(canonical['sources'][i]['content'][a:b].encode()).hexdigest(),
        segmentation='document_passages:c7f1cbca:exact-repeat-within-source:v1')
        for _, i, a, b, _ in windows]


def select(canonical, order, arm, fallback):
    data = select_order(deepcopy(canonical), order)
    if not fallback:
        data['selection']['method'] = 'experimental_' + arm
        data['selection']['relevance_semantics'] = 'semantic_candidate_not_answer_verification'
        for source in data['sources']:
            source['relevance'] = 'semantic_candidate' if source.get('highlights') or source.get('duplicate_evidence') else 'no_selected_candidate'
    return data


def compare(case, canonical, lexical, windows, response, documents):
    dense, hybrid = embedding_order(lexical, windows, response)
    eligible = [windows[i] for i in response['eligible']]
    diagnostics = dict(all_windows=measure(case, pool_output(canonical, windows), documents),
        lexical_candidates=measure(case, pool_output(canonical, lexical), documents),
        eligible_windows=measure(case, pool_output(canonical, eligible), documents))
    rows = []
    for arm, order in [('dense', dense), ('hybrid', hybrid)]:
        selected = select(canonical, order, arm, response['fallback'])
        pre = measure(case, selected, documents)
        for budget in (65536, 16384, 4096):
            final = finalize(deepcopy(selected), budget)
            metrics = measure(case, final, documents)
            rows.append(dict(id=case['id'], arm=arm, budget=budget, answerable=case['answerable'],
                wire_bytes=wire_bytes(final), window_count=len(windows), eligible_count=len(eligible),
                fallback=response['fallback'], pre_budget_success=pre['all_required_spans'],
                diagnostics=diagnostics, evidence=final,
                failure_classes=dict(
                    acquisition_or_segmentation=bool(case['answerable'] and diagnostics['all_windows']['gold_span_coverage'] < 1),
                    operational_window_eligibility=bool(case['answerable'] and diagnostics['eligible_windows']['gold_span_coverage'] < diagnostics['all_windows']['gold_span_coverage']),
                    ranking_or_three_source_cap=bool(case['answerable'] and diagnostics['eligible_windows']['all_required_spans'] and not pre['all_required_spans']),
                    atomic_budget=bool(case['answerable'] and pre['all_required_spans'] and not metrics['all_required_spans']),
                    query_or_request_fallback=bool(response['fallback']),
                    absent_unsupported_exposure=bool(not case['answerable'] and metrics['quote_count'])), **metrics))
    return rows


def evaluate_request(worker, case, canonical, repetition, reuse):
    """Complete bounded frozen selection request; shared main and probe seam."""
    started = time.perf_counter()
    replay_lexical, replay_windows = candidates(canonical, case['query'])
    metadata = identities(canonical, replay_windows)
    texts = [canonical['sources'][i]['content'][a:b] for _, i, a, b, _ in replay_windows]
    candidate_ms = (time.perf_counter()-started)*1000
    response = worker.score(case['query'], texts, identities=metadata, clear_cache=not reuse)
    started = time.perf_counter()
    lexical_selected = select_order(deepcopy(canonical), candidates(canonical, case['query'])[0])
    lexical_ms = (time.perf_counter()-started)*1000
    started = time.perf_counter()
    orders = embedding_order(replay_lexical, replay_windows, response)
    ordering_ms = (time.perf_counter()-started)*1000
    pipeline = []
    for arm, order in zip(('dense', 'hybrid'), orders):
        started = time.perf_counter()
        selected = select(canonical, order, arm, response['fallback'])
        selection_ms = (time.perf_counter()-started)*1000
        for budget in (65536, 16384, 4096):
            started = time.perf_counter()
            finalize(deepcopy(selected), budget)
            final_ms = (time.perf_counter()-started)*1000
            started = time.perf_counter()
            finalize(deepcopy(lexical_selected), budget)
            lexical_final_ms = (time.perf_counter()-started)*1000
            pipeline.append(dict(arm=arm, budget=budget,
                request_ms=candidate_ms+response['roundtrip_ms']+ordering_ms+selection_ms+final_ms,
                selection_ms=selection_ms, ordering_ms=ordering_ms, finalization_ms=final_ms,
                lexical_request_ms=lexical_ms+lexical_final_ms))
    return dict(id=case['id'], repetition=repetition, reuse=reuse,
        candidate_and_identity_ms=candidate_ms, pipeline=pipeline, **response)


def main():
    def cancelled(signum, frame):
        raise KeyboardInterrupt('Evaluation cancelled')
    signal.signal(signal.SIGTERM, cancelled)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker-python')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--library-path', default='')
    parser.add_argument('--worker-deadline', type=float, default=30)
    args = parser.parse_args()
    if bool(args.worker_python) != bool(args.model_dir):
        parser.error('worker Python and model directory must be supplied together')
    if not .1 <= args.worker_deadline <= 30:
        parser.error('deadline must be .1..30 seconds')
    corpus = load_corpus(DATA / 'corpus.json')
    baseline_path = DATA / 'results.json'
    baseline = json.loads(baseline_path.read_text())
    assert baseline['corpus_sha256'] == hashlib.sha256((DATA/'corpus.json').read_bytes()).hexdigest()
    documents, cases = corpus['documents'], corpus['cases']
    pools = {}
    rows = []
    for case in cases:
        canonical = dict(sources=[dict(deepcopy(documents[d]), cache_state='hit', stale=False)
            for d in case['documents']], content_mode='full_text', acquisition_limits=corpus['acquisition_limits'])
        lexical, windows = candidates(canonical, case['query'])
        pools[case['id']] = canonical, lexical, windows
        selected = select_order(deepcopy(canonical), lexical)
        for budget in (65536, 16384, 4096):
            final = finalize(deepcopy(selected), budget)
            frozen = next(c for c in baseline['cases'] if c['arm']=='lexical' and c['id']==case['id'] and c['budget']==budget)
            assert digest(final) == digest(frozen['evidence']), 'Frozen baseline drift'
    rows.extend(deepcopy(baseline['cases']))
    cold, costs, responses, peak, contracts = [], [], {}, 0, None
    model = None
    probe_spec = json.loads((DATA/'embedding-probes.json').read_text())
    probe_cases = [dict(deepcopy(next(c for c in cases if c['id']==p['gold_origin'])), **p) for p in probe_spec['cases']]
    probe_rows, probe_costs, probe_responses, probe_pools = [], [], {}, {}
    for case in probe_cases:
        canonical = deepcopy(pools[case['gold_origin']][0])
        lexical, windows = candidates(canonical,case['query'])
        probe_pools[case['id']] = canonical,lexical,windows
    if args.worker_python:
        manifest = json.loads((args.model_dir / 'manifest.json').read_text())
        assert manifest == json.loads((DATA/'embedding-model.json').read_text()), 'Unpinned manifest'
        for run in range(3):
            worker = Worker(args.worker_python, args.model_dir, args.library_path, args.worker_deadline)
            try:
                case = cases[3]
                canonical, lexical, windows = pools[case['id']]
                texts = [canonical['sources'][i]['content'][a:b] for _, i, a, b, _ in windows]
                first = worker.score(case['query'], texts, identities=identities(canonical, windows), verify_pooling=True)
                cold.append(dict(run=run, ready=worker.ready, first_case='04', first=first,
                    first_scope='qualified identifier fixture only; OS page cache not flushed'))
                if run != 2:
                    continue
                for repetition in range(10):
                    for case in cases:
                        canonical, lexical, windows = pools[case['id']]
                        for reuse in (False, True):
                            response = evaluate_request(worker,case,canonical,repetition,reuse)
                            responses[case['id']] = response
                            costs.append(response)
                for repetition in range(10):
                    for case in probe_cases:
                        canonical,_,_ = probe_pools[case['id']]
                        for reuse in (False,True):
                            response = evaluate_request(worker,case,canonical,repetition,reuse)
                            probe_responses[case['id']] = response
                            probe_costs.append(response)
                # Exercise exact-version isolation with real worker, not production hooks.
                contracts = version_contracts(worker, pools['04'], cases[3]['query'])
                peak = max(peak, worker.peak_rss_kib)
            finally:
                worker.close()
        assert all(c['first']['scores'] == cold[0]['first']['scores'] and c['first']['passage_misses'] == len(c['first']['eligible']) for c in cold)
        contracts['restart_recomputed_same_scores'] = True
        for case in cases:
            canonical, lexical, windows = pools[case['id']]
            rows.extend(compare(case, canonical, lexical, windows, responses[case['id']], documents))
        for case in probe_cases:
            canonical,lexical,windows = probe_pools[case['id']]
            response = probe_responses[case['id']]
            probe_rows.extend(compare(case,canonical,lexical,windows,response,documents))
            selected = select_order(deepcopy(canonical), lexical)
            for budget in (65536,16384,4096):
                final = finalize(deepcopy(selected),budget)
                probe_rows.append(dict(id=case['id'],arm='supplemental_lexical',budget=budget,answerable=case['answerable'],
                    wire_bytes=wire_bytes(final),evidence=final,**measure(case,final,documents)))
        model = dict(manifest=manifest, graph=json.loads((DATA/'embedding-graph.json').read_text()),
            cold=cold, warm=costs, supplemental_probe_warm=probe_costs, representation_contracts=contracts,
            representation_contract_scope='Worker vector identity/reuse/eviction only. Same-process clear and actual cold-worker restart recompute; score stability is NOT evidence-ID expansion proof. Exact emitted-ID refresh/restart/eviction separately tested through isolated canonical DocumentCache; inherited HTTP contracts are frozen corpus facts.', sampled_peak_rss_kib=peak,
            rss_sample_interval_ms=10, all_workers_reaped=True, concurrency=1, cpu_threads=1,
            token_limit_including_specials=256, truncation=False, batch_size=1,
            candidate_cap=128, representation_cap=1024, request_bytes=1024**2, response_bytes=65536,
            address_space_limit_bytes=2*1024**3, rss_limit_bytes=512*1024**2, cpu_seconds_limit=120,
            deadline_seconds=args.worker_deadline, artifact_payload_bytes=sum(f['bytes'] for f in manifest['files']),
            model_directory_bytes=sum(p.stat().st_size for p in args.model_dir.rglob('*') if p.is_file()),
            artifact_scope='selected complete primary ONNX/tokenizer/config/pooling/modules/license-card payload; directory count additionally includes local manifest; unused framework/export variants not downloaded',
            site_packages_bytes=sum(p.stat().st_size for p in Path(args.worker_python).absolute().parent.parent.glob('lib/python*/site-packages/**/*') if p.is_file()),
            footprint_scope='model payload vs optional site-packages vs executable/mapped native runtime are separate; maps overlap packages, not additive',
            requirements_lock_sha256=hashlib.sha256(Path(__file__).with_name('requirements.lock').read_bytes()).hexdigest())
        model['latency_scope'] = 'CPU-only wall time, one ORT thread, sequential; 3 cold processes and 10 warm/case/condition (160 frozen plus 20 supplemental-probe requests each); same-query miss->hit idealized second-call reuse, not natural retention; OS page cache not flushed; acquisition excluded; arm preparation sorts both orders and is charged to each arm, not claimed as isolated production latency'
        model['latency'] = []
        for reuse in (False, True):
            subset = [c for c in costs if c['reuse']==reuse]
            for field in ('tokenization_ms','query_encoding_ms','passage_encoding_miss_ms','passage_reuse_hit_ms','roundtrip_ms'):
                vals = [c[field] for c in subset if not c['fallback']]
                model['latency'].append(dict(field=field, reuse=reuse, n=len(vals), p50=percentile(vals,.5),p95=percentile(vals,.95)))
            for arm in ('dense','hybrid','lexical'):
                for budget in (65536,16384,4096):
                    vals = [p['lexical_request_ms' if arm=='lexical' else 'request_ms'] for c in subset for p in c['pipeline']
                        if p['budget']==budget and p['arm']==('dense' if arm=='lexical' else arm)]
                    model['latency'].append(dict(field='frozen_request_ms',arm=arm,budget=budget,reuse=reuse,
                        n=len(vals),p50=percentile(vals,.5),p95=percentile(vals,.95), acquisition_included=False))
    summary = []
    for arm in ('lexical','reranker','dense','hybrid'):
        for budget in (65536,16384,4096):
            subset = [c for c in rows if c['arm']==arm and c['budget']==budget and c['answerable']]
            if subset:
                summary.append(dict(arm=arm,budget=budget,n=len(subset),successes=sum(c['all_required_spans'] for c in subset),
                    qualified_recall=sum(c['qualification_complete_claim_recall'] for c in subset)/len(subset),
                    coverage_penalized_macro_precision_empty_as_zero=sum(c['relevant_quote_precision'] or 0 for c in subset)/len(subset),
                    nonempty_precision_cases=sum(c['relevant_quote_precision'] is not None for c in subset),
                    unsafe_claim_quotes=sum(c['unsafe_claim_quotes'] for c in subset),
                    quote_bytes=sum(c['quote_bytes'] for c in subset)))
    output = dict(schema_version=1, model=model, summary=summary, cases=rows,
        supplemental_probe_spec=probe_spec,supplemental_probe_cases=probe_rows,
        supplemental_probe_spec_sha256=hashlib.sha256((DATA/'embedding-probes.json').read_bytes()).hexdigest(),
        corpus_sha256=hashlib.sha256((DATA/'corpus.json').read_bytes()).hexdigest(),
        frozen_baseline_results_sha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        frozen_baseline_provenance=dict(ticket_revision='f5192bc073f2914ea9a6bd29c42fb3986960fa3d',
            integrated_revision='485181add1e46bfd9455e00a6cf6a2f50122013a',
            historical_runner_hashes=baseline['runner_hashes'],
            scope='ncn corpus/gold/results unchanged; historical hashes refer to pinned ncn code, NOT current evolved experiment files; reranker latency frozen, historical lexical selector latency paired in current runner'),
        runner_hashes={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('embedding_run.py','run.py','worker.py','selection.py','historical.py','acquire_model.py')},
        host=dict(platform=platform.platform(),python=platform.python_version(),cpu_affinity=sorted(os.sched_getaffinity(0))),
        acquisition_seconds=corpus['acquisition_seconds'], contracts=corpus['contracts'],
        workload='Frozen synthetic English HTML: 14 acquired documents/16 original queries plus 2 separately reported predeclared context-eligible paraphrase queries; no multilingual-support inference',
        policy='128 all coherent windows, no top-K before dense; atomic 256 total-token eligibility; whole-query overflow fallback; equal-weight RRF k=60; 3/source; no score threshold',
        recommendation='defer_production_adoption',
        recommendation_basis='Small synthetic transient-document pilot; measured gains/costs and unsupported absent exposure below, not a representative benchmark. No retained-content vector storage justified by this volume/reuse alone. Separate user approval required.')
    if model:
        pairs = {(c['id'],c['budget'],c['arm']):c for c in rows}
        output['paired_quality'] = []
        for arm in ('dense','hybrid'):
            for baseline_arm in ('lexical','reranker'):
                for budget in (65536,16384,4096):
                    before = [c for c in rows if c['arm']==baseline_arm and c['budget']==budget and c['answerable']]
                    improved = [c['id'] for c in before if not c['all_required_spans'] and pairs[c['id'],budget,arm]['all_required_spans']]
                    regressed = [c['id'] for c in before if c['all_required_spans'] and not pairs[c['id'],budget,arm]['all_required_spans']]
                    output['paired_quality'].append(dict(arm=arm,baseline=baseline_arm,budget=budget,n=len(before),improved=improved,regressed=regressed))
        output['absent_exposure'] = [dict(id=c['id'],arm=c['arm'],budget=c['budget'],quote_bytes=c['quote_bytes'],quotes=c['quote_count']) for c in rows if not c['answerable']]
        output['eligible_gold_recall'] = [dict(id=c['id'],window_count=c['window_count'],eligible_count=c['eligible_count'],
            all_window_qualified_recall=c['diagnostics']['all_windows']['qualification_complete_claim_recall'],
            eligible_qualified_recall=c['diagnostics']['eligible_windows']['qualification_complete_claim_recall'],
            lexical_candidate_qualified_recall=c['diagnostics']['lexical_candidates']['qualification_complete_claim_recall'],
            query_or_request_fallback=c['fallback']) for c in rows if c['arm']=='dense' and c['budget']==65536]
        identities_seen = {tuple(sorted(i.items())) for canonical,_,windows in pools.values() for i in identities(canonical,windows)}
        eligible_identities = {tuple(sorted(identities(canonical,windows)[i].items()))
            for case_id,(canonical,_,windows) in pools.items() for i in responses[case_id]['eligible']}
        model['workload_representation_volume'] = dict(acquired_documents=len(documents),
            canonical_bytes_including_duplicate_documents=sum(len(s['content'].encode()) for s in documents.values()),
            distinct_version_offset_text_recipe_windows=len(identities_seen),
            distinct_eligible_version_offset_text_recipe_windows=len(eligible_identities),
            eligible_vector_payload_if_all_retained_bytes=1536*len(eligible_identities),
            max_active_request_windows=max(len(p[2]) for p in pools.values()),
            max_observed_cache_entries=max(c.get('cache_entries',0) for c in costs),
            max_observed_vector_payload_bytes=max(c.get('vector_payload_bytes',0) for c in costs),
            vector_scope='384 float32 = 1536 bytes per eligible vector; payload excludes Python/cache keys/runtime overhead; miss->same-query hit pairs, not measured production reuse')
        output['supplemental_probe_summary'] = [dict(id=p['id'],gold_origin=p['gold_origin'],
            context_eligible_gold_recall=next(c['diagnostics']['eligible_windows']['qualification_complete_claim_recall'] for c in probe_rows if c['id']==p['id'] and c['arm']=='dense' and c['budget']==65536),
            lexical_candidate_gold_recall=next(c['diagnostics']['lexical_candidates']['qualification_complete_claim_recall'] for c in probe_rows if c['id']==p['id'] and c['arm']=='dense' and c['budget']==65536),
            outcomes=[dict(arm=c['arm'],budget=c['budget'],success=c['all_required_spans']) for c in probe_rows if c['id']==p['id']]) for p in probe_spec['cases']]
        output['recommendation_basis'] += (' Separate predeclared context-eligible short paraphrase probes test candidate inclusion only, not hard ranking; their outcomes are separately reported, not pooled into frozen baseline counts. '
            'At 64/16 KiB both new arms have ' + str(next(s['successes'] for s in summary if s['arm']=='dense' and s['budget']==65536)) +
            '/14 successes versus frozen lexical/reranker 12/14. Case03 qualifying gold has zero eligible-window recall under256; remaining semantic recall hypothesis is inconclusive, not evidence of a model ranking failure. '
            'Atomic eligibility losses and added absent-answer exposure prevent adoption from this recipe; no clipping or threshold tuning was used. '
            'Observed vector volume and immediate synthetic reuse savings do not justify a persistent vector DB/archive; keep representations process-local only.')
    args.output.write_text(json.dumps(output,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(summary,indent=2))


def version_contracts(worker, pool, query):
    canonical, _, windows = pool
    texts = [canonical['sources'][i]['content'][a:b] for _,i,a,b,_ in windows]
    original = identities(canonical, windows)
    first = worker.score(query,texts,identities=original,clear_cache=True)
    hit = worker.score(query,texts,identities=original)
    changed_text = texts[0].replace('TaskGroup', 'TaskGroupNew')
    if changed_text == texts[0]:
        changed_text = texts[0] + '\nNew version qualification.'
    changed = dict(original[0],version=hashlib.sha256(changed_text.encode()).hexdigest(),
        end=original[0]['start']+len(changed_text),text_sha256=hashlib.sha256(changed_text.encode()).hexdigest())
    refreshed = worker.score(query,[changed_text],identities=[changed])
    old = worker.score(query,texts,identities=original)
    gone = worker.score(query,texts,identities=original,evict_versions=[original[0]['version']],available_versions=[])
    recomputed = worker.score(query,texts,identities=original,clear_cache=True)
    assert first['passage_misses']==len(first['eligible']) and hit['passage_hits']==len(hit['eligible'])
    assert refreshed['passage_misses']==1 and old['scores']==first['scores']
    assert gone['eligible']==[] and gone['scores']==[]
    assert recomputed['passage_misses']==len(recomputed['eligible']) and recomputed['scores']==first['scores']
    return dict(refresh_new_representation=True, old_representation_same_scores=True, eviction_removes_eligibility=True,
        same_process_clear_recompute=True, no_production_cache_hooks=True,
        first=first,hit=hit,refreshed=refreshed,old=old,evicted=gone,recomputed=recomputed)


if __name__ == '__main__':
    main()
