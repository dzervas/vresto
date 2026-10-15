"""Harness mechanics with a deterministic encoder; no model/network required."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/evaluation'))
from highlights import compare, score as highlight_score
from memory import hit_rank, score
from test_highlight_evaluation import synthetic
from test_memory import FakeEmbedder


def test_global_hit_requires_labelled_source_and_exact_probe():
    corpus, labels = synthetic()
    case = labels['cases'][0]
    results = [dict(requested_url='https://other.org', highlights=[{'text': 'uncancel()'}]),
               dict(requested_url=corpus['documents']['task']['requested_url'], highlights=[{'text': 'not an answer'}]),
               dict(requested_url=corpus['documents']['task']['requested_url'], highlights=[{'text': 'uncancel()'}])]
    assert hit_rank(results, case, corpus['documents']) == 3
    assert hit_rank(results[:2], case, corpus['documents']) is None


def test_memory_paraphrases_do_not_rebless_historical_highlight_baseline():
    corpus, labels = synthetic()
    baseline = highlight_score(corpus, labels)
    extended = deepcopy(labels)
    extended['cases'].append(dict(id='paraphrase', sources=['task'], query='How do I erase interruption state?',
                                  expected=['uncancel()'], memory_paraphrase=True, response_budget_bytes=4096))
    report = highlight_score(corpus, extended)
    compare(report, baseline)
    assert report['aggregates'] == baseline['aggregates']
    assert len(extended['cases']) == 3


async def test_memory_harness_quality_and_cost_fields_with_fake_encoder(tmp_path, monkeypatch):
    import memory as harness
    class FakeWorker(FakeEmbedder):
        available = True
        rss_kib = 0
        def __init__(self, *args, **kwargs):
            pass
    monkeypatch.setattr(harness, 'WorkerEmbedder', FakeWorker)
    corpus, labels = synthetic()
    labels['cases'].append(dict(id='paraphrase', sources=['task'], query='How do I erase interruption state?',
        expected=['uncancel()'], memory_paraphrase=True, response_budget_bytes=4096))
    args = SimpleNamespace(corpus=tmp_path/'corpus.json', labels=tmp_path/'labels.json',
        store=tmp_path/'memory.sqlite', output=tmp_path/'metrics.json',
        model_dir=tmp_path, dimensions=768, int8=False, threads=4)
    args.corpus.write_text(json.dumps(corpus))
    args.labels.write_text(json.dumps(labels))
    await score(args)
    report = json.loads(args.output.read_text())
    assert report['metrics']['paraphrase']['hybrid']['hit@5'] == 1
    assert report['metrics']['original']['lexical']['hit@5'] == 1
    assert report['passages'] == 1
    assert report['store_bytes_per_1000_passages'] > 0
    assert report['added_memory_query_ms']['p50'] >= 0
    with pytest.raises(ValueError, match='fresh'):
        await score(args)
