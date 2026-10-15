"""Public packaging seam: the exact tar stream handed to docker build."""
import io
from pathlib import Path
import subprocess
import sys
import tarfile


def test_build_context_excludes_credentials_and_workspace_metadata(tmp_path):
    for path in ['Dockerfile', 'Dockerfile.headful', 'Dockerfile.chrome', 'pyproject.toml', 'uv.lock', 'NOTICE', '.dockerignore',
                 'src/kagi_crawler/server.py', 'src/kagi_crawler/sites/x.py',
                 'src/kagi_crawler/sites/__pycache__/x.cpython-312.pyc', 'licenses/LICENSE',
                 'licenses/embeddinggemma2-Apache-2.0.txt', 'scripts/install_embedding_model.py',
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
            'uv.lock', 'scripts/install_embedding_model.py', 'src/kagi_crawler/server.py', 'src/kagi_crawler/sites/x.py'}
    # An allowed filename must not smuggle an excluded file through a symlink.
    (tmp_path / 'src/kagi_crawler/server.py').unlink()
    (tmp_path / 'src/kagi_crawler/server.py').symlink_to(tmp_path / '.envrc')
    refused = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] /
        'scripts/build_context.py'), '--root', str(tmp_path)], capture_output=True)
    assert refused.returncode != 0
    assert b'build input symlink refused' in refused.stderr


def test_default_dockerfile_declares_owned_headed_chrome_only():
    # Configuration guard, not a claim that Debian/container launch was tested.
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / 'Dockerfile').read_text()
    # Google's apt package on both architectures (Patchright refuses arm64 chrome).
    assert 'ARG TARGETARCH' in dockerfile
    assert 'apt-get install -y --no-install-recommends google-chrome-stable' in dockerfile
    assert 'test -x /opt/google/chrome/chrome' in dockerfile
    assert 'KAGI_CRAWLER_HEADFUL=1' in dockerfile
    assert 'KAGI_CRAWLER_BROWSER_CHANNEL=chrome' in dockerfile
    assert '/etc/opt/chrome/policies/managed/native-doh.json' in dockerfile
    assert 'command -v Xvfb' in dockerfile
    assert 'chmod 1777 /tmp/.X11-unix' in dockerfile
    assert 'USER 10001:10001' in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "kagi_crawler.headful"]' in dockerfile
    assert 'patchright install --with-deps' not in dockerfile
    assert 'chrome_for_testing' not in dockerfile
    assert not (root / 'Dockerfile.headful').exists()
    assert not (root / 'Dockerfile.chrome').exists()
