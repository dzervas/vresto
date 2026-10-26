"""External Docker CLI fixture: no daemon, image, root or real credentials.

Inline image probes and display-wrapped harnesses run in real Python processes;
the fixture substitutes image identity/dependencies and a tiny release harness.
These tests verify driver behavior, not container/browser/deployment claims.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def docker_cli(tmp_path):
    commands = tmp_path / 'commands.jsonl'
    display_pids = tmp_path / 'display-pids'
    harness = tmp_path / 'release_check.py'
    harness.write_text('''import json, os, sys
from pathlib import Path
assert os.environ['DISPLAY'] == ':42'
assert os.environ['VRESTO_HEADFUL'] == '1'
assert sys.argv[1:] in (['--cache', '/data/cache'],
                       ['--cache', '/data/cache', '--verify-persisted'])
assert __name__ == '__main__'
with Path(os.environ['FIXTURE_HARNESS_LOG']).open('a') as log:
    log.write(json.dumps(sys.argv) + '\\n')
mode = os.environ['FIXTURE_MODE']
if mode == 'cold' and '--verify-persisted' not in sys.argv:
    raise RuntimeError('cold fixture failure')
if mode == 'persisted' and '--verify-persisted' in sys.argv:
    raise RuntimeError('persisted fixture failure')
''')
    executables = {
        'google-chrome': '''import os, sys
assert sys.argv[1:] == ['--version']
print('Chromium 153.0.8010.12' if os.environ['FIXTURE_MODE'] == 'browser' else 'Google Chrome 155.0.9999.7')
''',
        'kagi': '''import os, sys
assert sys.argv[1:] == ['--version']
print('kagi 0.21.0' if os.environ['FIXTURE_MODE'] == 'cli' else 'kagi 0.23.0')
''',
        'Xvfb': f'''import os, sys, time
from pathlib import Path
assert 'KAGI_SESSION_TOKEN' not in os.environ
assert 'VRESTO_ACCESS_TOKEN' not in os.environ
with Path({str(display_pids)!r}).open('a') as log:
    log.write(str(os.getpid()) + '\\n')
fd = int(sys.argv[sys.argv.index('-displayfd') + 1])
os.write(fd, b'42\\n')
os.close(fd)
time.sleep(60)
''',
        'docker': '''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['FIXTURE_COMMANDS']).open('a') as log:
    log.write(json.dumps(args) + '\\n')
mode = os.environ['FIXTURE_MODE']
if args[0] == 'version' and mode == 'daemon':
    sys.exit(19)
if args[0] in ('exec', 'stop') and mode == args[0]:
    sys.exit(19)
if args[0] == 'inspect':
    print({'exit_zero': '0', 'exit_kill': '137'}.get(mode, '143'))
if args[0] == 'run' and '--entrypoint' in args:
    index = args.index('--entrypoint')
    assert args[index + 1] == '/app/.venv/bin/python'
    assert args[index + 2] == 'fixture-image'
    assert args[index + 3] == '-c'
    code = args[index + 4]
    if 'virtual_display' in code:
        rest = [os.environ['FIXTURE_HARNESS'], *args[index + 6:]]
    else:
        # Only the image identity/dependency boundary is simulated. Run the
        # actual version probe against external Chrome and kagi executables.
        code = ("import os,importlib.metadata as m; "
                "os.getuid=lambda: 0 if os.environ['FIXTURE_MODE']=='uid' else 10001; "
                "m.distributions=lambda: [type('D',(),{'metadata': {'Name': 'Torch'}})()] "
                "if os.environ['FIXTURE_MODE']=='models' else []; " + code)
        rest = []
    sys.exit(subprocess.run([sys.executable, '-c', code, *rest]).returncode)
''',
    }
    for name, code in executables.items():
        executable = tmp_path / name
        executable.write_text(f'#!{sys.executable}\n' + code)
        executable.chmod(0o700)
    env = {**os.environ, 'PATH': str(tmp_path),
           'FIXTURE_COMMANDS': str(commands),
           'FIXTURE_HARNESS': str(harness), 'FIXTURE_HARNESS_LOG': str(tmp_path / 'harness.jsonl'),
           'VRESTO_HEADFUL': '1', 'VRESTO_BROWSER_CHANNEL': 'chrome',
           'KAGI_SESSION_TOKEN': 'fixture-session', 'VRESTO_ACCESS_TOKEN': 'fixture-access'}
    return tmp_path, commands, display_pids, env


@pytest.mark.parametrize('mode', ['success', 'daemon', 'uid', 'missing_browser', 'browser', 'cli', 'models',
                                  'cold', 'persisted', 'exec', 'stop', 'exit_zero', 'exit_kill'])
def test_container_gate_commands_outcomes_and_cleanup(docker_cli, mode):
    directory, journal, display_pids, env = docker_cli
    if mode == 'missing_browser':
        (directory / 'google-chrome').unlink()
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/container_check.py'),
                             'fixture-image'], env={**env, 'FIXTURE_MODE': mode},
                            capture_output=True, text=True, timeout=20)
    calls = [json.loads(line) for line in journal.read_text().splitlines()]
    assert calls[0] == ['version', '--format', '{{.Server.Version}}']
    if mode == 'daemon':
        assert result.returncode != 0
        assert len(calls) == 1
        return
    assert calls[1][:2] == ['volume', 'create']
    volume = calls[1][2]
    assert volume.startswith('kagi-gate-')
    assert calls[-1] == ['volume', 'rm', volume]
    runs = [call for call in calls if call[0] == 'run' and '--entrypoint' in call]
    for call in runs:
        assert call[1:12] == ['--rm', '--init', '--network', 'none', '--cap-drop', 'ALL',
                              '--security-opt', 'no-new-privileges', '--shm-size', '256m', '--mount']
        assert call[12] == f'type=volume,src={volume},dst=/data/cache'
        assert call[13] == '--mount'
        assert call[14] == f'type=bind,src={ROOT / "scripts"},dst=/gate,readonly'
    harness_log = directory / 'harness.jsonl'
    invocations = [json.loads(line) for line in harness_log.read_text().splitlines()] if harness_log.exists() else []
    expected_harnesses = 0 if mode in ('uid', 'missing_browser', 'browser', 'cli', 'models') else 1 if mode == 'cold' else 2
    assert len(invocations) == expected_harnesses
    if invocations:
        assert invocations[0][1:] == ['--cache', '/data/cache']
        assert runs[1][-3:] == ['/gate/release_check.py', '--cache', '/data/cache']
    if len(invocations) == 2:
        assert invocations[1][1:] == ['--cache', '/data/cache', '--verify-persisted']
        assert runs[2][-4:] == ['/gate/release_check.py', '--cache', '/data/cache', '--verify-persisted']
    pids = display_pids.read_text().splitlines() if display_pids.exists() else []
    assert len(pids) == expected_harnesses
    assert all(not psutil.pid_exists(int(pid)) for pid in pids)
    entries = [call for call in calls if call[0] == 'run' and '--detach' in call]
    if expected_harnesses == 2 and mode != 'persisted':
        assert len(entries) == 1
        entry = entries[0]
        name = entry[entry.index('--name') + 1]
        assert entry == ['run', '--detach', '--name', name, '--init', '--network', 'none',
                         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                         '--env', 'VRESTO_ACCESS_TOKEN=fixture-access',
                         '--env', 'KAGI_SESSION_TOKEN=fixture-session', 'fixture-image']
        assert calls[-2] == ['rm', '--force', '--volumes', name]
        if mode not in ('exec', 'stop'):
            assert ['stop', '--time', '10', name] in calls
            assert ['inspect', '--format', '{{.State.ExitCode}}', name] in calls
    else:
        assert not entries
    if mode == 'success':
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report == {'browser_path': str(directory / 'google-chrome'),
                          'browser_version': 'Google Chrome 155.0.9999.7'}
    else:
        assert result.returncode != 0
        if mode in ('exit_zero', 'exit_kill'):
            assert 'production headful SIGTERM did not exit with 143' in result.stderr
    assert 'fixture-session' not in result.stdout + result.stderr
    # CalledProcessError includes the exec argv and its synthetic Bearer token.
    # This driver never receives real credentials; do not claim redaction here.
    if mode != 'exec':
        assert 'fixture-access' not in result.stdout + result.stderr
    else:
        assert 'returned non-zero exit status 19' in result.stderr
