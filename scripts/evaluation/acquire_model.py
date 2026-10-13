"""Parent-only public acquisition; never imported by the offline worker.

Run with project Python: acquire_model.py /tmp/kagi-ncn-model. Only the six
primary-publisher pinned files below are requested. HF redirects deliver public
LFS bytes; no hub cache, token discovery, credentials or remote Python code.
"""
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import urlopen

REVISION = '15192f12d2403ed1fd7c16a8841379096c5d4a9b'
MODEL = 'cross-encoder/ms-marco-TinyBERT-L2-v2'
WEIGHT_SHA256 = '0eac39ee56a3edf98d0beee17fa1bb368a5a1d8c8671b5afd6dd71677f8d8496'
FILES = {'onnx/model.onnx': 17604619, 'tokenizer.json': 711396,
         'tokenizer_config.json': 1330, 'special_tokens_map.json': 732,
         'config.json': 843, 'README.md': 3570}


def main():
    destination = Path(sys.argv[1])
    inventory = []
    expected = {f['path']: f for f in json.loads((Path(__file__).parent / 'data/model.json').read_text())['files']}
    for name, size in FILES.items():
        url = f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}'
        with urlopen(url, timeout=60) as response:
            payload = response.read(size + 1)
        if len(payload) != size:
            raise ValueError(f'Unexpected artifact size: {name}')
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected[name]['sha256'] or (name == 'onnx/model.onnx' and digest != WEIGHT_SHA256):
            raise ValueError(f'Pinned artifact hash mismatch: {name}')
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        inventory.append(dict(path=name, bytes=size, sha256=digest, url=url))
    if 'license: apache-2.0' not in (destination / 'README.md').read_text():
        raise ValueError('Publisher license declaration changed')
    manifest = dict(model=MODEL, revision=REVISION, license='apache-2.0',
                    license_basis='primary pinned README declaration; no standalone LICENSE',
                    files=inventory)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
