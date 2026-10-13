"""Optional experiment seams: frozen HTTP corpus and model-free replay CLI."""
import json
import subprocess
import sys
from pathlib import Path
import hashlib
import os
import signal
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/evaluation'))

ROOT = Path(__file__).resolve().parents[1]


def test_model_free_replay_keeps_gold_and_budget_contracts(tmp_path):
    output = tmp_path / 'results.json'
    subprocess.run([sys.executable, str(ROOT / 'scripts/evaluation/run.py'),
                    '--output', str(output)], check=True)
    data = json.loads(output.read_text())
    assert len(data['cases']) == 48
    assert all(c['wire_bytes'] <= c['budget'] for c in data['cases'])
    assert all(c['verbatim_version_bound'] for c in data['cases'])
    absent = [c for c in data['cases'] if c['id'] == '15']
    assert all(c['quote_bytes'] == 0 and c['candidate_count'] == 0 for c in absent)
    assert data['model'] is None
    discrimination = next(c for c in data['cases'] if c['id'] == '02' and c['budget'] == 65536)
    assert discrimination['candidate_count'] > 3 and discrimination['all_window_coverage'] == 1
    assert all(data['contracts'].values())
    missed = next(c for c in data['cases'] if c['id'] == '03' and c['budget'] == 65536)
    assert missed['candidate_coverage'] == 0 and missed['all_window_coverage'] == 1


def test_replay_refuses_gold_for_a_different_canonical_version(tmp_path):
    corpus = json.loads((ROOT / 'scripts/evaluation/data/corpus.json').read_text())
    corpus['cases'][0]['gold'][0]['content_version'] = '0' * 64
    path = tmp_path / 'corpus.json'
    path.write_text(json.dumps(corpus))
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/evaluation/run.py'),
        '--corpus', str(path), '--output', str(tmp_path / 'result.json')], capture_output=True)
    assert result.returncode != 0
    assert b'Invalid frozen gold' in result.stderr


async def test_real_http_freeze_keeps_complete_canonical_and_historical_evidence(tmp_path):
    from freeze import capture
    corpus = await capture(tmp_path / 'corpus.json', tmp_path / 'cache')
    assert len(corpus['documents']) == 14 and len(corpus['cases']) == 16
    assert all(corpus['contracts'].values())
    expanded = corpus['documents']['I']
    assert 'Native expanded cancellation caveat.' in expanded['content']
    assert 'cancel_called is not reliable before version 3.13.' in expanded['content']
    assert expanded['coverage']['exhaustive'] is False
    assert expanded['content'].count('Initial discussion.') == 1
    for item in corpus['fixture_manifest']:
        html = (ROOT / 'scripts/evaluation/data' / (item['id'] + '.html')).read_bytes()
        assert item['html_sha256'] == hashlib.sha256(html).hexdigest()


@pytest.mark.parametrize('cancel', [False, True])
def test_offline_runtime_is_reaped_on_deadline_or_cancellation(tmp_path, cancel):
    # Executable fixture at the external model-runtime boundary, no internal mocks.
    pid_file = tmp_path / 'pid'
    executable = tmp_path / 'blocked-runtime'
    executable.write_text(f'#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n'
        f'Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
        'print("{\\"ready\\":true}",flush=True)\ntime.sleep(60)\n')
    executable.chmod(0o700)
    (tmp_path / 'manifest.json').write_bytes((ROOT / 'scripts/evaluation/data/model.json').read_bytes())
    command = [sys.executable, str(ROOT / 'scripts/evaluation/run.py'),
        '--worker-python', str(executable), '--model-dir', str(tmp_path),
        '--worker-deadline', '30' if cancel else '0.5',
        '--output', str(tmp_path / 'result.json')]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            end = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < end and process.poll() is None:
                time.sleep(0.01)
            assert pid_file.exists(), process.communicate(timeout=5)
            if cancel:
                process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=5)
            assert process.returncode != 0
            if not cancel:
                assert b'Worker deadline exceeded' in stderr
            pid = int(pid_file.read_text())
            assert not Path(f'/proc/{pid}').exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_rescore_order_is_shared_with_source_offset_ties_and_whole_query_fallback():
    from selection import rescore_order
    pool = [(5.0, 1, 100, 150, 'first'), (4.0, 0, 200, 250, 'second'),
            (3.0, 0, 100, 150, 'third'), (2.0, 2, 10, 50, 'fourth')]
    scored = rescore_order(pool, {'scores': [1.0, 1.0, 1.0, 2.0], 'fallback': None})
    assert scored == [pool[3], pool[2], pool[1], pool[0]]
    assert rescore_order(pool, {'scores': [], 'fallback': 'whole_query_lexical'}) == pool


@pytest.mark.skipif(not os.environ.get('KAGI_EVALUATION_WORKER_PYTHON'), reason='isolated pinned CPU worker is opt-in')
def test_actual_optional_cpu_runner_never_truncates_and_retains_paired_contracts(tmp_path):
    output = tmp_path / 'measured.json'
    subprocess.run([sys.executable, str(ROOT / 'scripts/evaluation/run.py'),
        '--worker-python', os.environ['KAGI_EVALUATION_WORKER_PYTHON'],
        '--model-dir', os.environ['KAGI_EVALUATION_MODEL_DIR'],
        '--library-path', os.environ.get('LD_LIBRARY_PATH', ''), '--output', str(output)],
        check=True, timeout=120)
    data = json.loads(output.read_text())
    model = data['model']
    assert len(model['cold']) == 3 and len(model['warm']) == 160
    assert all(c['ready']['offline_probe'] for c in model['cold'])
    assert model['all_workers_reaped'] and model['truncation'] is False
    assert len(data['cases']) == 96
    assert all(c['verbatim_version_bound'] and c['wire_bytes'] <= c['budget'] for c in data['cases'])
    discriminate = [c for c in model['warm'] if c['id'] == '02']
    assert all(len(c['lengths']) > 3 and max(c['lengths']) <= 512 and not c['fallback'] for c in discriminate)
    oversized = [c for c in model['warm'] if c['id'] == '05']
    assert all(c['fallback'] == 'whole_query_lexical' and max(c['lengths']) > 512 and not c['scores'] for c in oversized)
    baseline = {(c['id'], c['budget']): c for c in data['cases'] if c['arm'] == 'lexical'}
    for case in [c for c in data['cases'] if c['fallback']]:
        assert case['evidence'] == baseline[case['id'], case['budget']]['evidence']
