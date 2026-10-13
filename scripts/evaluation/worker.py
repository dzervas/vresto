"""Offline text-only CPU worker. No imports of project/service/discovery code.

Linux x86_64 seccomp denies socket creation/connect, fork/vfork and exec;
clone/clone3 remain available for trusted ORT threads (not a hostile-code sandbox).
ordinary-user rlimits bound address space/CPU/fds. This is a trusted pinned
runtime, not a hostile-code filesystem sandbox. Parent kills/reaps on deadline,
cancellation, output or RSS limits. Worker never receives URLs or credentials.
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
    for line in sys.stdin:
        if len(line.encode()) > 1024**2:
            raise ValueError('Request exceeds one MiB')
        request = json.loads(line)
        query, passages = request['query'], request['passages']
        if not isinstance(query, str) or len(query) > 8192 or len(passages) > 128:
            raise ValueError('Query/candidate limit')
        if any(not isinstance(p, str) or len(p.encode()) > 262144 for p in passages):
            raise ValueError('Passage limit')
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
