"""Offline text-only CPU worker. No imports of project/service/discovery code.

Linux x86_64 seccomp denies socket creation/connect, fork/vfork and exec;
clone/clone3 remain available for trusted ORT threads (not a hostile-code sandbox).
ordinary-user rlimits bound address space/CPU/fds. This is a trusted pinned
runtime, not a hostile-code filesystem sandbox. Parent kills/reaps on deadline,
cancellation, output or RSS limits. Worker receives no service configuration or
credentials; verbatim public canonical passages may contain public link text.
"""
import ctypes
import errno
import hashlib
import json
from pathlib import Path
import platform
import resource
import sys
import time


def constrain():
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Offline worker requires Linux x86_64 seccomp')
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]
    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]
    # Kill wrong ABI rather than allowing another syscall numbering convention.
    rules = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e), (0x06, 0, 0, 0x80000000),
             (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000),
             (0x06, 0, 0, 0x80000000)]  # Reject x32 syscall numbering too.
    for syscall in (41, 42, 53, 57, 58, 59, 322):
        rules += [(0x15, 0, 1, syscall), (0x06, 0, 0, 0x50000 | errno.EPERM)]
    rules += [(0x06, 0, 0, 0x7fff0000)]
    filters = (Filter * len(rules))(*(Filter(*rule) for rule in rules))
    program = Program(len(rules), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.byref(program), 0, 0):
        raise OSError(ctypes.get_errno(), 'seccomp could not be installed')
    import socket
    try:
        socket.socket()
    except PermissionError:
        return
    raise RuntimeError('Network containment probe failed')


def emit(data):
    print(json.dumps(data, separators=(',', ':')), flush=True)


def embedding_response(request, tokenizer, session, np, cache, artifact_identity):
    """Process-local exact-version representations; no persistence or project hooks."""
    if request.get('clear_cache'):
        cache.clear()
    query, passages, identities = request['query'], request['passages'], request['identities']
    if len(identities) != len(passages):
        raise ValueError('Missing canonical representation identities')
    recipe = 'bert-single-specials:no-truncation:256:attention-mask-mean:L2:float32:384:v1'
    keys = []
    for text, identity in zip(passages, identities):
        if (set(identity) != {'version', 'start', 'end', 'text_sha256', 'segmentation'} or
            len(identity['version']) != 64 or identity['start'] < 0 or
            identity['end'] - identity['start'] != len(text) or
            identity['text_sha256'] != hashlib.sha256(text.encode()).hexdigest()):
            raise ValueError('Invalid representation version/offset/text hash')
        keys.append((identity['version'], identity['start'], identity['end'],
                     identity['text_sha256'], identity['segmentation'], artifact_identity, recipe))
    for identity in request.get('evict_versions', []):
        for key in list(cache):
            if key[0] == identity:
                del cache[key]
    available = set(request.get('available_versions', [k[0] for k in keys]))
    start = time.perf_counter()
    qencoding = tokenizer.encode(query)
    encodings = [tokenizer.encode(p) for p in passages]
    lengths = [len(e.ids) for e in encodings]
    token_ms = (time.perf_counter()-start)*1000
    eligible = [i for i, n in enumerate(lengths) if n <= 256 and keys[i][0] in available]

    def encode(encoding, padding=0):
        feed = {'input_ids': np.array([encoding.ids + [0]*padding], dtype=np.int64),
                'attention_mask': np.array([encoding.attention_mask + [0]*padding], dtype=np.int64),
                'token_type_ids': np.array([encoding.type_ids + [0]*padding], dtype=np.int64)}
        hidden = session.run(None, feed)[0]
        if hidden.shape != (1, len(encoding.ids)+padding, 384) or not np.isfinite(hidden).all():
            raise ValueError('Unexpected/nonfinite embedding graph output')
        mask = feed['attention_mask'][..., None]
        vector = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
        vector = vector.astype(np.float32)
        norm = np.linalg.norm(vector)
        if not np.isfinite(norm) or norm <= 0:
            raise ValueError('Invalid embedding norm')
        vector = (vector / norm)[0]
        assert abs(float(np.linalg.norm(vector))-1) < 1e-5
        return vector

    query_ms = miss_ms = hit_ms = 0.0
    hits = misses = 0
    scores = []
    overflow = len(qencoding.ids) > 256
    fallback = 'whole_query_lexical' if overflow else None
    if not overflow:
        start = time.perf_counter()
        qvector = encode(qencoding)
        query_ms = (time.perf_counter()-start)*1000
        for i in eligible:
            start = time.perf_counter()
            if keys[i] in cache:
                vector = cache[keys[i]]
                hits += 1
                hit_ms += (time.perf_counter()-start)*1000
            else:
                vector = encode(encodings[i])
                # Bounded FIFO; expiry loses only reuse, never evidence availability.
                if len(cache) >= 1024:
                    del cache[next(iter(cache))]
                cache[keys[i]] = vector
                misses += 1
                miss_ms += (time.perf_counter()-start)*1000
            scores.append(float(np.dot(qvector, vector)))
    diagnostic = None
    if request.get('verify_pooling'):
        example = tokenizer.encode('A short English sentence.')
        vector = encode(example)
        padded = encode(example, 11)
        delta = float(np.max(np.abs(vector-padded)))
        assert delta < 1e-5
        diagnostic = dict(dimension=384, finite=True, unit_norm=float(np.linalg.norm(vector)),
                          padding_max_abs_delta=delta, padding_invariant=True,
                          pooling='attention-mask-aware mean then L2; graph emits token embeddings')
    return dict(scores=scores, lengths=lengths, query_length=len(qencoding.ids), eligible=eligible,
        excluded_overflow=[i for i,n in enumerate(lengths) if n > 256],
        unavailable=[i for i,k in enumerate(keys) if k[0] not in available],
        overflow=overflow, fallback=fallback, tokenization_ms=token_ms,
        query_encoding_ms=query_ms, passage_encoding_miss_ms=miss_ms,
        passage_reuse_hit_ms=hit_ms, passage_hits=hits, passage_misses=misses,
        cache_entries=len(cache), vector_payload_bytes=sum(v.nbytes for v in cache.values()),
        inference_ms=query_ms+miss_ms+hit_ms, pooling_verification=diagnostic,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def main():
    began = time.perf_counter()
    constrain()
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    directory = Path(sys.argv[1])
    manifest = json.loads((directory / 'manifest.json').read_text())
    for item in manifest['files']:
        payload = (directory / item['path']).read_bytes()
        if len(payload) != item['bytes'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
            raise ValueError('Artifact hash/size mismatch')
    # No automatic from_pretrained lookup, remote code, truncation or padding.
    tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    assert tokenizer.encode('a', 'b').ids == [101, 1037, 102, 1038, 102]
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(str(directory / 'onnx/model.onnx'), options,
                                   providers=['CPUExecutionProvider'])
    assert session.get_providers() == ['CPUExecutionProvider']
    assert [i.name for i in session.get_inputs()] == ['input_ids', 'attention_mask', 'token_type_ids']
    assert len(session.get_outputs()) == 1
    startup_ms = (time.perf_counter()-began)*1000
    # Runtime inventory is separate from model payload and site-packages footprint;
    # mapped files overlap installed packages and must NOT be added twice.
    runtime_paths = {Path(line.split()[-1]) for line in Path('/proc/self/maps').read_text().splitlines()
                     if line.split()[-1].startswith('/')}
    runtime_files = [dict(path=str(p), bytes=p.stat().st_size) for p in sorted(runtime_paths) if p.is_file()]
    emit(dict(ready=True, startup_ms=startup_ms, mapped_runtime_files=runtime_files,
        executable=dict(path=str(Path(sys.executable).resolve()), bytes=Path(sys.executable).stat().st_size),
        dependency_versions={d.metadata['Name']: d.version for d in __import__('importlib.metadata', fromlist=['distributions']).distributions()},
        versions=dict(numpy=np.__version__, onnxruntime=ort.__version__,
                      tokenizers=__import__('tokenizers').__version__, python=platform.python_version()),
        inputs=[dict(name=i.name, shape=i.shape, type=i.type) for i in session.get_inputs()],
        outputs=[dict(name=i.name, shape=i.shape, type=i.type) for i in session.get_outputs()],
        offline_probe=True, cpu_threads=1, batch_size=1,
        idle_rss_kib=int(Path('/proc/self/status').read_text().split('VmRSS:')[1].split()[0])))
    embedding = manifest['model'] == 'sentence-transformers/all-MiniLM-L6-v2'
    if embedding:
        assert tokenizer.encode('a').ids == [101, 1037, 102]
        assert json.loads((directory / 'sentence_bert_config.json').read_text())['max_seq_length'] == 256
        pooling = json.loads((directory / '1_Pooling/config.json').read_text())
        assert pooling == dict(word_embedding_dimension=384, pooling_mode_cls_token=False,
            pooling_mode_mean_tokens=True, pooling_mode_max_tokens=False,
            pooling_mode_mean_sqrt_len_tokens=False)
        modules = json.loads((directory / 'modules.json').read_text())
        assert [m['type'].split('.')[-1] for m in modules] == ['Transformer', 'Pooling', 'Normalize']
        assert session.get_outputs()[0].shape[-1] == 384
    cache = {}
    artifact_identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    for line in sys.stdin:
        if len(line.encode()) > 1024**2:
            raise ValueError('Request exceeds one MiB')
        request = json.loads(line)
        query, passages = request['query'], request['passages']
        if not isinstance(query, str) or len(query) > 8192 or len(passages) > 128:
            raise ValueError('Query/candidate limit')
        if any(not isinstance(p, str) or len(p.encode()) > 262144 for p in passages):
            raise ValueError('Passage limit')
        if embedding:
            emit(embedding_response(request, tokenizer, session, np, cache, artifact_identity))
            continue
        start = time.perf_counter()
        encodings = [tokenizer.encode(query, passage) for passage in passages]
        lengths = [len(e.ids) for e in encodings]
        token_ms = (time.perf_counter()-start)*1000
        overflow = any(n > 512 for n in lengths)
        scores = []
        start = time.perf_counter()
        if not overflow:
            for encoding in encodings:
                feed = {'input_ids': np.array([encoding.ids], dtype=np.int64),
                        'attention_mask': np.array([encoding.attention_mask], dtype=np.int64),
                        'token_type_ids': np.array([encoding.type_ids], dtype=np.int64)}
                value = session.run(None, feed)[0]
                assert value.shape == (1, 1) and np.isfinite(value).all()
                scores.append(float(value[0, 0]))  # Raw logits, never support probabilities.
        emit(dict(scores=scores, lengths=lengths, overflow=overflow,
            fallback='whole_query_lexical' if overflow else None,
            tokenization_ms=token_ms, inference_ms=(time.perf_counter()-start)*1000,
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))


if __name__ == '__main__':
    main()
