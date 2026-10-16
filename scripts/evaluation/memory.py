"""Offline memory Hit@k vs FTS5-only over the entire frozen corpus.

Run: uv run --locked python scripts/evaluation/run.py memory \
  --model-dir .runtime/embeddinggemma2 --store .runtime/memory-eval.sqlite \
  --output /tmp/kc-memory-metrics.json
The store must not exist: throughput includes cold worker startup and all ingest.
Hit means at least one expected literal probe in a top-k passage from a labelled
source. This is passage retrieval, not budgeted highlight qualification recall.

5950X / four threads / FP32 768d, 37 frozen documents (1355 bounded passages):
original Hit@5 hybrid 18/24 vs FTS5 19/24; paraphrases 3/10 vs 4/10. This run
shows no Hit@5 uplift; keep the production lexical highlighter independent.
Cold ingest including writes: 1.36 passages/s, worker peak RSS 1.67 GiB;
7.04 MiB/1000 passages including canonical versions/FTS/vectors. Warm added
memory-query p50/p95 was 40/50 ms, excluding browser/upstream work. These are
local pilot observations, not capacity or quality guarantees.
"""
import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import resource
import time

from vresto.evidence import resolve_evidence
from vresto.memory import Memory, MemoryStore, WorkerEmbedder, bounded_passages

ROOT = Path(__file__).resolve().parents[2]


def percentile(values, p):
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * p))]


def hit_rank(results, case, documents):
    urls = {documents[sid]['requested_url'] for sid in case['sources'] if 'error' not in documents[sid]}
    return next((i for i, result in enumerate(results, 1)
        if result['requested_url'] in urls and any(probe in result['highlights'][0]['text'] for probe in case['expected'])), None)


async def score(args):
    corpus_bytes = args.corpus.read_bytes()
    label_bytes = args.labels.read_bytes()
    documents = json.loads(corpus_bytes)['documents']
    cases = [c for c in json.loads(label_bytes)['cases'] if c['expected']]
    for case in cases:
        for probe in case['expected']:
            assert any(probe in documents[sid].get('content', '') for sid in case['sources']), (case['id'], probe)
    if args.store.exists():
        raise ValueError('Use a fresh --store path for a reproducible ingest measurement')
    embedder = WorkerEmbedder(args.model_dir, dimensions=args.dimensions, int8=args.int8, threads=args.threads)
    if not embedder.available:
        raise ValueError('Acquire the pinned model explicitly first')
    store = MemoryStore(args.store, embedder.model_id)
    memory = Memory(store, embedder)
    try:
        started = time.perf_counter()
        successful = [s for s in documents.values() if 'error' not in s]
        expected_count = sum(len(list(bounded_passages(s))) for s in successful)
        for i, source in enumerate(successful, 1):
            memory.submit(source)
            await memory.queue.join()
            print(f'ingested {i}/{len(successful)} documents; {store.usage()["passages"]} passages', flush=True)
        ingest_seconds = time.perf_counter() - started
        usage = store.usage()
        assert memory.failures == memory.drops == 0, memory.usage()
        assert usage['passages'] == expected_count
        rows, latencies, lexical_latencies = [], [], []
        for case in cases:
            started = time.perf_counter()
            hybrid = await memory.query(case['query'], 10, set())
            latencies.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            lexical = await asyncio.to_thread(store.query, case['query'], None, 10)
            lexical_latencies.append((time.perf_counter() - started) * 1000)
            for result in hybrid + lexical:
                reference = result['highlights'][0]
                canonical, start, end = resolve_evidence(store, reference['evidence_id'])
                assert canonical['content'][start:end] == reference['text']
            rows.append(dict(id=case['id'], paraphrase=case.get('memory_paraphrase', False),
                hybrid_rank=hit_rank(hybrid, case, documents), lexical_rank=hit_rank(lexical, case, documents)))
        assert memory.failures == 0, memory.usage()
        metrics = {}
        for group in ('all', 'original', 'paraphrase'):
            selected = [r for r in rows if group == 'all' or r['paraphrase'] == (group == 'paraphrase')]
            metrics[group] = {'cases': len(selected)}
            for arm in ('hybrid', 'lexical'):
                metrics[group][arm] = {f'hit@{k}': sum(r[arm+'_rank'] is not None and r[arm+'_rank'] <= k for r in selected)
                                      for k in (1, 5, 10)}
        output = dict(schema_version=1, model_id=embedder.model_id, threads=args.threads,
            corpus_sha256=hashlib.sha256(corpus_bytes).hexdigest(), labels_sha256=hashlib.sha256(label_bytes).hexdigest(),
            cpu=next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                      if line.startswith('model name')), platform.processor()), cpu_count=os.cpu_count(),
            versions={package: version(package) for package in ('onnxruntime', 'numpy', 'tokenizers')},
            documents=len(successful), passages=usage['passages'], ingest_seconds=ingest_seconds,
            passages_per_second=usage['passages']/ingest_seconds, worker_peak_rss_kib=embedder.rss_kib,
            parent_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            store_bytes=usage['bytes'], store_bytes_per_1000_passages=usage['bytes']*1000/usage['passages'],
            added_memory_query_ms={f'p{p}': percentile(latencies, p/100) for p in (50, 95)},
            lexical_query_ms={f'p{p}': percentile(lexical_latencies, p/100) for p in (50, 95)},
            latency_scope='warm serial memory query added after live discovery/acquisition; no browser or upstream time',
            quality_scope='global corpus; best expected probe passage; not budgeted highlighter recall',
            metrics=metrics, cases=rows)
        args.output.write_text(json.dumps(output, indent=2) + '\n')
        print(json.dumps({k: v for k, v in output.items() if k != 'cases'}, indent=2))
    finally:
        await memory.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=ROOT / 'eval/corpus/corpus.json')
    parser.add_argument('--labels', type=Path, default=ROOT / 'eval/highlight_cases.json')
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dimensions', type=int, choices=(128, 256, 512, 768), default=768)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--int8', action='store_true')
    asyncio.run(score(parser.parse_args()))


if __name__ == '__main__':
    main()
