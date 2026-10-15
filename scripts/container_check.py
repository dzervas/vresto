"""Operator release gate: python scripts/container_check.py kagi-crawler:local.

Requires a reachable Docker daemon; never starts one or requests privileges.
Uses two fresh unprivileged containers and one named volume, no real credentials.
Mounted harness is test-only; neither it nor fixture exceptions are in the image.
Native observations do not verify image build, Debian shared libraries, container
Google Chrome launch/seccomp, UID volume ownership, cgroup peaks or signal handling.
This gate is intentionally separate from those claims until actually executed.
"""
import argparse
from pathlib import Path
import subprocess
import uuid


def docker(*arguments, **kwargs):
    return subprocess.run(['docker', *arguments], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    args = parser.parse_args()
    docker('version', '--format', '{{.Server.Version}}')
    volume = 'kagi-gate-' + uuid.uuid4().hex
    harness = Path(__file__).resolve().parent
    docker('volume', 'create', volume, stdout=subprocess.DEVNULL)
    try:
        common = ['run', '--rm', '--init', '--network', 'none', '--cap-drop', 'ALL',
                  '--security-opt', 'no-new-privileges', '--shm-size', '256m',
                  '--mount', f'type=volume,src={volume},dst=/data/cache',
                  '--mount', f'type=bind,src={harness},dst=/gate,readonly',
                  '--entrypoint', '/app/.venv/bin/python', args.image]
        # Network=none still permits the explicitly allowlisted loopback fixtures.
        # Check actual installed CLI, identity and dependency closure without auth.
        docker(*common, '-c', 'import os,subprocess,importlib.metadata as m; '
               'assert os.getuid()==10001; '
               'import shutil,re,json; '
               'browser=shutil.which("google-chrome"); assert browser, "Google Chrome is not installed"; '
               'version=subprocess.check_output([browser,"--version"],text=True).strip(); '
               'assert re.fullmatch(r"Google Chrome \\d+\\.\\d+\\.\\d+\\.\\d+",version), version; '
               'print(json.dumps({"browser_path":browser,"browser_version":version}),flush=True); '
               'assert subprocess.check_output(["kagi","--version"],text=True).strip()=="kagi 0.22.0"; '
               'assert not {"torch","transformers","sentence-transformers","onnxruntime"} & '
               '{d.metadata["Name"].lower() for d in m.distributions()}')
        # Overriding the image entry point bypasses its display ownership. Own one
        # here too: the image defaults to headed Chrome, not bundled Chromium.
        # Keep run_path's script argv and let virtual_display clean up on errors.
        wrapper = ('import runpy,sys; from kagi_crawler.headful import virtual_display; '
                   'sys.argv=sys.argv[1:]; '
                   '\nwith virtual_display(): runpy.run_path(sys.argv[0],run_name="__main__")')
        docker(*common, '-c', wrapper, '/gate/release_check.py', '--cache', '/data/cache')
        docker(*common, '-c', wrapper, '/gate/release_check.py', '--cache', '/data/cache',
               '--verify-persisted')
        # The mounted release harness checks search/fetch/expand/crawl schemas and calls.
        # Exercise the production entry point, private HTTP and SIGTERM too.
        name = 'kagi-entry-' + uuid.uuid4().hex
        docker('run', '--detach', '--name', name, '--init', '--network', 'none',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--env', 'KAGI_CRAWLER_ACCESS_TOKEN=fixture-access',
               '--env', 'KAGI_SESSION_TOKEN=fixture-session', args.image,
               stdout=subprocess.DEVNULL)
        try:
            docker('exec', name, '/app/.venv/bin/python', '-c',
                'import time,httpx; '
                'time.sleep(3); '
                'r=httpx.get("http://127.0.0.1:8000/health",trust_env=False); '
                'assert r.status_code==401; '
                'r=httpx.get("http://127.0.0.1:8000/health",headers={"Authorization":"Bearer fixture-access"},trust_env=False); '
                'assert r.status_code==200 and r.json()["status"]=="ok"')
            docker('stop', '--time', '10', name, stdout=subprocess.DEVNULL)
            result = subprocess.check_output(['docker', 'inspect', '--format', '{{.State.ExitCode}}', name], text=True)
            # virtual_display deliberately exits 128 + SIGTERM after cleanup.
            assert result.strip() == '143', 'production headful SIGTERM did not exit with 143'
        finally:
            subprocess.run(['docker', 'rm', '--force', '--volumes', name], check=False,
                           stdout=subprocess.DEVNULL)
    finally:
        docker('volume', 'rm', volume, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
