import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path

import pytest

from kagi_crawler.memory import (Memory, MemoryStore, WorkerEmbedder, bounded_passages,
                                 document_input)


def source(text='A feline rests on the sofa.', url='https://example.org/cats', fetched_at=None):
    return dict(requested_url=url, resolved_url=url, title='Cats', document_type='html',
                fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(), content=text,
                content_version=hashlib.sha256(text.encode()).hexdigest(), content_mode='full_text')


class FakeEmbedder:
    model_id = 'fake/2'

    async def encode(self, text):
        return [1., 0.] if any(word in text.lower() for word in ('cat', 'feline', 'kitten')) else [0., 1.]

    async def close(self):
        pass


def store(tmp_path, **kwargs):
    return MemoryStore(tmp_path / 'memory.sqlite', 'fake/2', **kwargs)


def test_store_persists_deduplicates_and_keeps_multiple_passages(tmp_path):
    db = store(tmp_path)
    s = source('First feline.\n\nSecond feline.')
    for a, b in ((0, 13), (15, len(s['content']))):
        db.put(s, a, b, None, [1., 0.])
        db.put(s, a, b, None, [1., 0.])
    assert db.usage()['passages'] == 2
    db.close()
    db = store(tmp_path)
    assert db.lookup(s['requested_url'], s['content_version']) == s
    assert len(db.query('feline', [1., 0.])) == 2
    db.close()


def test_hybrid_fts_escape_exclusions_and_model_staleness(tmp_path):
    db = store(tmp_path)
    a, b = source(), source('A canine chases a ball.', 'https://example.org/dogs')
    db.put(a, 0, len(a['content']), None, [1., 0.])
    db.put(b, 0, len(b['content']), None, [0., 1.])
    assert db.query('kitten', [1., 0.])[0]['requested_url'] == a['requested_url']
    assert db.query('canine', None)[0]['requested_url'] == b['requested_url']
    assert db.query('" OR * : (', None) == []
    assert not db.query('cat', [1., 0.], exclude={a['requested_url'], b['requested_url']})
    alias = source('A feline alias.', 'https://example.org/alias')
    alias['resolved_url'] = a['resolved_url']
    db.put(alias, 0, len(alias['content']), None, [1., 0.])
    assert not db.query('cat', [1., 0.], exclude={a['requested_url'], b['requested_url']})
    db.close()
    changed = MemoryStore(tmp_path / 'memory.sqlite', 'new-model')
    assert changed.query('feline', [1., 0.]) == []
    assert changed.lookup(a['requested_url'], a['content_version']) == a
    changed.close()


def test_age_and_physical_cap_evict_oldest(tmp_path):
    db = store(tmp_path, cap_bytes=100_000)
    old = source('Old passage', fetched_at='2000-01-01T00:00:00+00:00')
    db.put(old, 0, 11, None, [1., 0.])
    for i in range(8):
        s = source('Long prose ' * 1000, f'https://example.org/{i}')
        db.put(s, 0, len(s['content']), None, [1., 0.])
    assert db.usage()['bytes'] <= 100_000
    assert db.lookup(old['requested_url'], old['content_version']) is None
    db.close()
    aged = MemoryStore(tmp_path / 'aged.sqlite', 'fake/2', retention_seconds=60)
    aged.put(old, 0, 11, None, [1., 0.])
    assert aged.lookup(old['requested_url'], old['content_version']) is None
    assert aged.usage()['passages'] == 0
    assert aged.query('Old', [1., 0.]) == []  # Cascaded eviction removes FTS entries too.
    aged.close()


def test_cap_reclaims_fts_tombstones_even_when_one_document_cannot_fit(tmp_path):
    db = store(tmp_path, cap_bytes=100_000)
    s = source(' '.join(f'unique{i:08d}' for i in range(20_000)))
    db.put(s, 0, len(s['content']), None, [1., 0.])
    assert db.usage()['passages'] == 0
    assert db.usage()['bytes'] <= 100_000
    assert db.lookup(s['requested_url'], s['content_version']) is None
    db.close()


def test_passages_bound_unicode_and_skip_navigation():
    s = source('# Topic\n\n' + '猫' * 10_000)
    passages = list(bounded_passages(s))
    assert len(passages) > 1
    assert all(len(s['content'][a:b].encode()) <= 6000 for a, b, _ in passages)
    assert passages[0][0] == 0 and passages[-1][1] == len(s['content'])
    assert not list(bounded_passages(source('[one](https://one.org) [two](https://two.org)')))
    assert len(document_input('猫' * 1000, 'p').encode()) < 600


async def test_background_queue_drop_dedupe_and_close(tmp_path):
    memory = Memory(store(tmp_path), FakeEmbedder(), queue_capacity=1)
    memory.submit(source())
    memory.submit(source())
    assert memory.drops == 1
    await memory.queue.join()
    memory.submit(source())
    await memory.queue.join()
    assert memory.usage()['passages'] == 1
    assert (await memory.query('kitten', 5, set()))[0]['origin'] == 'memory'
    await memory.close()
    assert memory.task.done()


async def test_identical_reacquisition_refreshes_date_without_expiring_duplicates(tmp_path):
    db = store(tmp_path)
    old = source(fetched_at='2000-01-01T00:00:00+00:00')
    db.put(old, 0, len(old['content']), None, [1., 0.])
    db.retention_seconds = 60
    memory = Memory(db, FakeEmbedder())
    fresh = source()
    memory.submit(fresh)
    await memory.queue.join()
    assert memory.usage()['passages'] == 1
    assert db.lookup(fresh['requested_url'], fresh['content_version'])['fetched_at'] == fresh['fetched_at']
    assert (await memory.query('feline', 5, set()))[0]['fetched_at'] == fresh['fetched_at']
    await memory.close()


async def test_ingest_failure_is_counted_and_does_not_stop_queue(tmp_path):
    class Broken(FakeEmbedder):
        async def encode(self, text):
            raise ValueError('fixed failure')
    memory = Memory(store(tmp_path), Broken())
    memory.submit(source())
    await memory.queue.join()
    assert memory.failures == 1
    assert await memory.query('cat', 5, set()) == []
    await memory.close()


async def test_query_timeout_fails_open(tmp_path):
    class Slow(FakeEmbedder):
        async def encode(self, text):
            await asyncio.sleep(10)
    memory = Memory(store(tmp_path), Slow())
    assert await memory.query('cat', 5, set(), timeout_seconds=.01) == []
    assert memory.failures == 1
    await memory.close()


async def test_raw_asyncio_cancellation_during_spawn_reaps_worker(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    opened = asyncio.Event()
    children = []
    async def delayed_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        opened.set()
        await asyncio.sleep(.05)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
    embedder = WorkerEmbedder(tmp_path / 'absent')
    task = asyncio.create_task(embedder.encode('cat'))
    await opened.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert embedder.process is None
    assert children[0].returncode is not None


MODEL = Path('.runtime/embeddinggemma2')


@pytest.mark.parametrize('int8,dimensions', [(False, 768), (True, 256)])
async def test_real_model_worker_isolated_normalized_and_bounded(int8, dimensions):
    embedder = WorkerEmbedder(MODEL, int8=int8, dimensions=dimensions)
    if not embedder.available:
        pytest.skip('pinned local model absent')
    try:
        vector = await embedder.encode('task: search result | query: a pet cat')
        assert len(vector) == dimensions
        assert sum(x*x for x in vector) == pytest.approx(1, abs=1e-5)
        assert embedder.process.pid != __import__('os').getpid()
        assert embedder.rss_kib < 3 * 1024 * 1024
        with pytest.raises(KeyError):
            await embedder.encode('cat ' * 10_000)
    finally:
        await embedder.close()
    assert embedder.process is None
