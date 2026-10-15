"""Anonymous, checksum-verified acquisition of the pinned public ONNX export."""
import argparse
import hashlib
from pathlib import Path
import urllib.request

REVISION = 'daa72c51243991dfcaf9f9137d2c573d8f7790c0'
BASE = f'https://huggingface.co/onnx-community/embeddinggemma-2-ONNX/resolve/{REVISION}'
FILES = {
    'tokenizer.json': '4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4',
    'onnx/model.onnx': 'bc47de15f81208a5c99e5ab10f746d5e33b51ea228b7dc0bef9c133a94f1c1c3',
    'onnx/model.onnx_data': '9fd452bfc6916e5a92f080f8dada35598ca4512b21c65897d4c5974d5f9630a2',
    'onnx/model_quantized.onnx': 'd06edd601f851c633a2519304cbeb8dc6170d7ceb61b436625c17fb9b6e74953',
    'onnx/model_quantized.onnx_data': '278a7ff1248c3618e4bd11a607fc54f7bdc7778854230f3956d3f86bd9db4f3b',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--int8', action='store_true')
    args = parser.parse_args()
    variant = 'model_quantized' if args.int8 else 'model'
    for name in ('tokenizer.json', f'onnx/{variant}.onnx', f'onnx/{variant}.onnx_data'):
        target = args.directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + '.tmp')
        digest = hashlib.sha256()
        with urllib.request.urlopen(f'{BASE}/{name}', timeout=120) as response, temporary.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != FILES[name]:
            temporary.unlink()
            raise ValueError(f'Checksum mismatch: {name}')
        temporary.replace(target)
        print(name, digest.hexdigest(), flush=True)


if __name__ == '__main__':
    main()
