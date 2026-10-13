"""Emit an allowlisted build tar, without traversing workspace/credential trees.

python scripts/build_context.py | docker build -t kagi-crawler:local -
.dockerignore independently protects ordinary docker build . invocations too.
Reject symlinks: a link from an allowed path must not read anything outside it.
"""
import argparse
from pathlib import Path
import sys
import tarfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
root = parser.parse_args().root
files = [root / name for name in ('Dockerfile', 'Dockerfile.headful', 'Dockerfile.chrome', '.dockerignore', 'pyproject.toml', 'uv.lock')]
files += list((root / 'src/kagi_crawler').glob('*.py'))
# The Dockerfiles no longer copy licenses/: do not require or traverse the
# removed directory. Keep the context aligned with the current image inputs.
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    for path in sorted(files):
        if any(part.is_symlink() for part in [path, *path.parents]):
            raise ValueError('build input symlink refused')
        if not path.is_file():
            raise ValueError('required build input missing or not a regular file')
        archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
