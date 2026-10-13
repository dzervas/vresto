"""Optional evaluation seams: deterministic ranking, CLI and isolated worker."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/evaluation'))


def test_hybrid_rrf_keeps_zero_lexical_candidates_without_support_threshold():
    from selection import embedding_order
    lexical = [(4., 0, 0, 10, 'a'), (3., 0, 10, 20, 'b')]
    windows = lexical + [(0., 0, 20, 30, 'c')]
    response = {'scores': [.1, .2, .9], 'eligible': [0, 1, 2], 'fallback': None}
    dense, hybrid = embedding_order(lexical, windows, response)
    assert [p[2] for p in dense] == [20, 10, 0]
    # a: 1/61+1/63; b: 2/62; c: 1/61. Retain c (zero BM25).
    assert [p[2] for p in hybrid] == [0, 10, 20]
    assert embedding_order(lexical, windows, dict(response, fallback='whole_query_lexical')) == (lexical, lexical)
    excluded = dict(response, eligible=[0, 2], scores=[.1, .9])
    dense, hybrid = embedding_order(lexical, windows, excluded)
    assert [p[2] for p in dense] == [20, 0]
    assert [p[2] for p in hybrid] == [0, 20]


def test_dense_ties_use_source_offset_order_with_eligible_filter_and_early_fallback():
    from selection import embedding_order, rescore_order
    windows = [(0., 0, 0, 10, 'excluded'), (0., 1, 0, 10, 'later source'),
               (0., 0, 20, 30, 'later offset'), (0., 0, 10, 20, 'first')]
    lexical = [windows[0], windows[1]]
    response = dict(scores=[1., 1., 1.], eligible=[1, 2, 3], fallback=None)
    dense, hybrid = embedding_order(lexical, windows, response)
    assert dense == [windows[3], windows[2], windows[1]]
    assert hybrid == [windows[1], windows[3], windows[2]]
    assert dense == rescore_order([windows[1], windows[2], windows[3]], response)
    # Whole-query fallback must not try to inspect missing eligibility/scores.
    assert embedding_order(lexical, windows, {'fallback': 'whole_query_lexical'}) == (lexical, lexical)


def test_embedding_model_free_cli_retains_frozen_baseline_without_model_deps(tmp_path):
    output = tmp_path/'model-free.json'
    subprocess.run([sys.executable, str(ROOT/'scripts/evaluation/embedding_run.py'),
        '--output', str(output)], check=True)
    result = json.loads(output.read_text())
    frozen = ROOT/'scripts/evaluation/data/results.json'
    assert result['model'] is None and len(result['cases'])==96
    assert result['cases']==json.loads(frozen.read_text())['cases']
    assert result['frozen_baseline_results_sha256']==hashlib.sha256(frozen.read_bytes()).hexdigest()


@pytest.mark.skipif(not os.environ.get('KAGI_EMBEDDING_MODEL_DIR'), reason='isolated pinned embedding runtime opt-in')
def test_actual_embedding_worker_respects_overflow_identity_eviction_and_restart():
    from run import Worker
    directory = os.environ['KAGI_EMBEDDING_MODEL_DIR']
    python = os.environ['KAGI_EVALUATION_WORKER_PYTHON']
    libraries = os.environ.get('LD_LIBRARY_PATH', '')
    text = 'TaskGroup preserves cancellation warnings in Python 3.13.'
    version = hashlib.sha256(text.encode()).hexdigest()
    identity = dict(version=version,start=0,end=len(text),text_sha256=version,segmentation='test-v1')
    def score(worker, texts=None, ids=None, **kw):
        return worker.score('cancellation warning', texts or [text], identities=ids or [identity], **kw)
    worker = Worker(python,directory,libraries)
    try:
        first = score(worker,verify_pooling=True)
        assert worker.ready['offline_probe'] and first['pooling_verification']['padding_invariant']
        assert first['pooling_verification']['dimension']==384 and first['passage_misses']==1
        hit = score(worker)
        assert hit['passage_hits']==1 and hit['scores']==first['scores']
        # Same text/offset under a different document version is a new representation.
        new = score(worker,ids=[dict(identity,version='1'*64)])
        assert new['passage_misses']==1
        # Segmentation is also versioned, even if this one passage is unchanged.
        assert score(worker,ids=[dict(identity,segmentation='test-v2')])['passage_misses']==1
        long = 'word '*255  # 255 content + CLS/SEP => 257 total; never clipped.
        long_id = dict(version='2'*64,start=0,end=len(long),text_sha256=hashlib.sha256(long.encode()).hexdigest(),segmentation='test-v1')
        oversized = score(worker,[text,long],[identity,long_id])
        assert oversized['eligible']==[0] and oversized['excluded_overflow']==[1]
        assert oversized['lengths'][1]==257 and len(oversized['scores'])==1
        fits = 'word '*254  # Exactly 256 including specials is eligible.
        fit_id = dict(long_id,version='3'*64,end=len(fits),text_sha256=hashlib.sha256(fits.encode()).hexdigest())
        boundary = score(worker,[fits],[fit_id])
        assert boundary['lengths']==[256] and boundary['eligible']==[0] and len(boundary['scores'])==1
        overflow = worker.score(long,[text],identities=[identity])
        assert overflow['fallback']=='whole_query_lexical' and not overflow['scores'] and overflow['query_length']==257
        evicted = score(worker,available_versions=[],evict_versions=[version])
        assert evicted['eligible']==[] and evicted['scores']==[] and evicted['unavailable']==[0]
        excessive = score(worker,[text]*129,[identity]*129)
        assert excessive['fallback']=='candidate_or_request_limit' and excessive['eligible']==[]
    finally:
        worker.close()
    restarted = Worker(python,directory,libraries)
    try:
        after = score(restarted)
        assert after['passage_misses']==1 and after['scores']==first['scores']
    finally:
        restarted.close()
    assert not Path(f'/proc/{worker.process.pid}').exists()
    assert not Path(f'/proc/{restarted.process.pid}').exists()


@pytest.mark.skipif(not os.environ.get('KAGI_EMBEDDING_MODEL_DIR'), reason='isolated pinned embedding runtime opt-in')
def test_actual_embedding_cli_returns_expandable_evidence_and_measured_sidecars(tmp_path):
    from copy import deepcopy
    from datetime import datetime, timezone
    from kagi_crawler.cache import DocumentCache
    from kagi_crawler.evidence import expand_evidence
    output = tmp_path/'measured.json'
    subprocess.run([sys.executable, str(ROOT/'scripts/evaluation/embedding_run.py'),
        '--output', str(output), '--worker-python', os.environ['KAGI_EVALUATION_WORKER_PYTHON'],
        '--model-dir', os.environ['KAGI_EMBEDDING_MODEL_DIR'],
        '--library-path', os.environ.get('LD_LIBRARY_PATH','')], check=True, timeout=120)
    result = json.loads(output.read_text())
    assert len(result['model']['cold'])==3 and len(result['model']['warm'])==320
    assert len(result['model']['supplemental_probe_warm'])==40
    assert len(result['cases'])==192 and len(result['supplemental_probe_cases'])==18
    assert all(c['context_eligible_gold_recall']==1 for c in result['supplemental_probe_summary'])
    assert all(c['wire_bytes']<=c['budget'] and c['verbatim_version_bound'] for c in result['supplemental_probe_cases'])
    assert all(c['first']['pooling_verification']['padding_invariant'] for c in result['model']['cold'])
    assert all(c['wire_bytes']<=c['budget'] and c['verbatim_version_bound'] for c in result['cases'])
    assert result['model']['representation_contracts']['restart_recomputed_same_scores']
    corpus = json.loads((ROOT/'scripts/evaluation/data/corpus.json').read_text())
    sources = deepcopy(corpus['documents'])
    cache = DocumentCache(tmp_path/'canonical-only')
    for s in sources.values():
        s['fetched_at'] = datetime.now(timezone.utc).isoformat()
        cache.store(s)
    returned = [(s,q) for c in result['cases'] if c['arm'] in ('dense','hybrid') for s in c['evidence']['sources'] for q in s.get('highlights',[])]
    assert returned
    # Public exact-version expansion seam, including zero-lexical experimental IDs.
    for _, q in returned:
        expanded = expand_evidence(cache,[q['evidence_id']])
        assert expanded['sources'][0]['highlights'][0]['text']==q['text']
        assert 'score' not in q
    source, quote = returned[0]
    old = next(s for s in sources.values() if s['requested_url']==source['requested_url'])
    changed = dict(old,content=old['content']+'\nNew version qualification.')
    changed['content_version'] = hashlib.sha256(changed['content'].encode()).hexdigest()
    cache.store(changed)
    restart = DocumentCache(tmp_path/'canonical-only')
    assert expand_evidence(restart,[quote['evidence_id']])['sources'][0]['highlights'][0]['text']==quote['text']
    evicted = DocumentCache(tmp_path/'canonical-only',cap_bytes=0)
    assert expand_evidence(evicted,[quote['evidence_id']])['sources'][0]['error']['code']=='evidence_unavailable'


@pytest.mark.skipif(not os.environ.get('KAGI_EMBEDDING_MODEL_DIR'), reason='isolated pinned embedding runtime opt-in')
def test_bounded_embedding_request_runner_completes_over_candidate_cap():
    from copy import deepcopy
    from embedding_run import evaluate_request
    from run import Worker
    corpus = json.loads((ROOT/'scripts/evaluation/data/corpus.json').read_text())
    canonical = dict(sources=[dict(deepcopy(corpus['documents']['B']),cache_state='hit',stale=False) for _ in range(65)],
        content_mode='full_text',acquisition_limits=corpus['acquisition_limits'])
    worker = Worker(os.environ['KAGI_EVALUATION_WORKER_PYTHON'],os.environ['KAGI_EMBEDDING_MODEL_DIR'],os.environ.get('LD_LIBRARY_PATH',''))
    try:
        measured = evaluate_request(worker,dict(id='over-cap',query='cancellation'),canonical,0,False)
        assert measured['fallback']=='candidate_or_request_limit' and measured['eligible']==[]
        assert len(measured['pipeline'])==6 and all(p['request_ms']>=0 for p in measured['pipeline'])
        assert measured['query_encoding_ms']==0 and not measured['scores']
    finally:
        worker.close()
