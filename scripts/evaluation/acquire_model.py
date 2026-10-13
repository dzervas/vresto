"""Parent-only public acquisition; never imported by the offline worker.

Run with project Python: acquire_model.py /tmp/kagi-ncn-model, or
acquire_model.py /tmp/kagi-anl-model --embedding. Only the six reranker or ten
embedding primary-publisher files in the checked manifest are requested. HF
redirects deliver public LFS bytes; no hub cache, token discovery, credentials
or remote Python code. Independently verified hashes bind each entire bundle.
"""
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import urlopen

def main():
    destination = Path(sys.argv[1])
    inventory = []
    embedding = sys.argv[2:] == ['--embedding']
    pinned = json.loads((Path(__file__).parent / 'data' /
        ('embedding-model.json' if embedding else 'model.json')).read_text())
    expected = {f['path']: f for f in pinned['files']}
    for name, item in expected.items():
        size = item['bytes']
        url = f"https://huggingface.co/{pinned['model']}/resolve/{pinned['revision']}/{name}"
        with urlopen(url, timeout=60) as response:
            payload = response.read(size + 1)
        if len(payload) != size:
            raise ValueError(f'Unexpected artifact size: {name}')
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected[name]['sha256']:
            raise ValueError(f'Pinned artifact hash mismatch: {name}')
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        inventory.append(dict(path=name, bytes=size, sha256=digest, url=url))
    if 'license: apache-2.0' not in (destination / 'README.md').read_text():
        raise ValueError('Publisher license declaration changed')
    manifest = dict(pinned, files=inventory)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
