"""Public packaging seam: the exact tar stream handed to docker build."""
import io
from pathlib import Path
import subprocess
import sys
import tarfile


def test_build_context_excludes_credentials_and_workspace_metadata(tmp_path):
    for path in ['Dockerfile', 'pyproject.toml', 'uv.lock', 'NOTICE', '.dockerignore',
                 'src/kagi_crawler/server.py', 'licenses/Crawl4AI-LICENSE',
                 '.envrc', '.beads/issues.json', '.jj/repo/config.toml',
                 'src/kagi_crawler/.envrc', 'src/kagi_crawler/credentials.json',
                 'licenses/session.key']:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('synthetic fixture only')
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] /
        'scripts/build_context.py'), '--root', str(tmp_path)], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        assert set(archive.getnames()) == {'.dockerignore', 'Dockerfile', 'pyproject.toml',
            'uv.lock', 'NOTICE', 'src/kagi_crawler/server.py', 'licenses/Crawl4AI-LICENSE'}
    # An allowed filename must not smuggle an excluded file through a symlink.
    (tmp_path / 'src/kagi_crawler/server.py').unlink()
    (tmp_path / 'src/kagi_crawler/server.py').symlink_to(tmp_path / '.envrc')
    refused = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] /
        'scripts/build_context.py'), '--root', str(tmp_path)], capture_output=True)
    assert refused.returncode != 0
    assert b'build input symlink refused' in refused.stderr
