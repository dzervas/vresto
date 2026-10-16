"""Private line-protocol embedding worker; no network or automatic model download."""
import json
from pathlib import Path
import resource
import sys


def main():
    directory, variant, threads, memory_bytes = sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_AS, (int(memory_bytes), int(memory_bytes)))
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    options = ort.SessionOptions()
    options.intra_op_num_threads = int(threads)
    options.inter_op_num_threads = 1
    # Do not retain peak-size arenas between requests.
    options.enable_cpu_mem_arena = False
    session = ort.InferenceSession(str(Path(directory) / 'onnx' / f'{variant}.onnx'),
                                   options, providers=['CPUExecutionProvider'])
    tokenizer = Tokenizer.from_file(str(Path(directory) / 'tokenizer.json'))
    for line in sys.stdin:
        request = json.loads(line)
        encoding = tokenizer.encode(request['text'])
        if len(encoding.ids) > 8192:
            print(json.dumps({'error': 'token_limit'}), flush=True)
            continue
        feed: dict[str, np.ndarray] = {'input_ids': np.array([encoding.ids], np.int64),
                'attention_mask': np.array([encoding.attention_mask], np.int64)}
        for key in ('image_features', 'video_features', 'audio_features'):
            feed[key] = np.empty((0, 512), np.float32)
        vector = np.asarray(session.run(['sentence_embedding'], feed)[0])[0][:request['dimensions']]
        vector = vector / np.linalg.norm(vector)
        print(json.dumps({'vector': vector.tolist(),
                          'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)


if __name__ == '__main__':
    main()
