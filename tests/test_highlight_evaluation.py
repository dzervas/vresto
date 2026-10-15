"""Offline real-page harness contracts; no browser or network."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/evaluation'))
from highlights import compare, score, union_bytes


def synthetic():
    text = '# Cancellation\n\nTask cancellation requires uncancel() to remove state. café.\n'
    source = dict(requested_url='https://example.org/task', resolved_url='https://example.org/task',
                  document_type='html', content=text, content_mode='full_text',
                  content_version=hashlib.sha256(text.encode()).hexdigest(),
                  fetched_at='2026-10-15T00:00:00+00:00', title='Task',
                  coverage={'partial': False, 'exhaustive': True})
    corpus = dict(documents={'task': source}, cases=[dict(id=cid, sources=['task'])
                                                   for cid in ('answer', 'absent')])
    labels = dict(schema_version=1, cases=[
        dict(id='answer', sources=['task'], query='Task cancellation',
             expected=['uncancel()', 'café'], response_budget_bytes=4096),
        dict(id='absent', sources=['task'], query='zzyy_unlisted_feature_918274',
             expected=[], response_budget_bytes=4096)])
    return corpus, labels


def test_highlight_scorer_offline_cli_and_comparison(tmp_path):
    corpus, labels = synthetic()
    cp, lp, baseline = (tmp_path / name for name in ('corpus.json', 'labels.json', 'baseline.json'))
    cp.write_text(json.dumps(corpus))
    lp.write_text(json.dumps(labels))
    command = [sys.executable, str(ROOT / 'scripts/evaluation/run.py'), 'highlights', 'score',
               '--corpus', str(cp), '--cases', str(lp)]
    first = subprocess.run([*command, '--save-baseline', str(baseline)], check=True,
                           capture_output=True, text=True)
    report = json.loads(first.stdout)
    answer, absent = report['cases']
    assert answer['recall'] == 1 and answer['answer_bearing_share'] == 1
    assert answer['highlight_bytes'] == len(corpus['documents']['task']['content'].encode())
    assert answer['overlap_bytes'] == 0 and answer['highlight_count'] == 1
    assert answer['first_section'] == 'Cancellation'
    assert absent['recall'] is None and absent['highlight_bytes'] == 0
    assert report['aggregates']['macro_recall'] == 1
    second = subprocess.run([*command, '--compare', str(baseline)], check=True,
                            capture_output=True, text=True)
    assert all(v == 0 for row in json.loads(second.stdout)['cases'] for v in row['delta'].values())


@pytest.mark.parametrize('tamper', ['version', 'expected', 'sources', 'comparison'])
def test_highlight_scorer_rejects_invalid_reference(tamper):
    corpus, labels = synthetic()
    if tamper == 'version':
        corpus['documents']['task']['content_version'] = 'bad'
    elif tamper == 'expected':
        labels['cases'][0]['expected'] = ['not present']
    elif tamper == 'sources':
        labels['cases'][0]['sources'] = []
    else:
        baseline = score(corpus, labels)
        labels['cases'][0]['query'] = 'different'
        with pytest.raises(ValueError, match='different corpus/expectations'):
            compare(score(corpus, labels), baseline)
        return
    with pytest.raises(ValueError):
        score(corpus, labels)


def test_overlap_counts_utf8_union_not_character_lengths():
    assert union_bytes('éabc', [(0, 2), (1, 3), (1, 2)]) == len('éab'.encode())
