"""Independent retained passage history, bounded background ingestion and hybrid search.

One process owns the SQLite file. Dense scans are O(passages * dimensions), not
ANN: keep this to tens of thousands of passages; larger deployments need ANN.
Model changes mark old vectors inactive (no automatic re-embedding); their exact
versions remain expandable until retention evicts them. Reacquisition reindexes.
"""
import asyncio
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime
import json
from itertools import islice
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Protocol

import anyio
import numpy as np

from .evidence import document_passages, evidence_id, page_references, _link_density_penalty
from .process import Processes
from .types import SourceSuccess, ToolData

REVISION = 'daa72c51243991dfcaf9f9137d2c573d8f7790c0'


class Embedder(Protocol):
    model_id: str

    async def encode(self, text: str) -> list[float]: ...
    async def close(self) -> None: ...


class WorkerEmbedder:
    def __init__(self, directory: Path, *, int8: bool = False, dimensions: int = 768,
                 threads: int = 4, memory_bytes: int = 6_442_450_944):
        self.directory = directory.resolve()
        self.variant = 'model_quantized' if int8 else 'model'
        self.dimensions = dimensions
        self.threads = threads
        self.memory_bytes = memory_bytes
        self.model_id = f'{REVISION}/{self.variant}/{dimensions}'
        self.process = None
        self.lock = asyncio.Lock()
        self.rss_kib = 0

    @property
    def available(self):
        return all(path.is_file() for path in (self.directory / 'tokenizer.json',
            self.directory / 'onnx' / f'{self.variant}.onnx',
            self.directory / 'onnx' / f'{self.variant}.onnx_data'))

    async def encode(self, text: str) -> list[float]:
        async with self.lock:
            try:
                async with asyncio.timeout(60):
                    if self.process is None:
                        env = {key: os.environ[key] for key in ('PATH', 'LD_LIBRARY_PATH', 'LANG') if key in os.environ}
                        env.update(HOME='/nonexistent', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
                                   TOKENIZERS_PARALLELISM='false')
                        with anyio.CancelScope(shield=True):
                            spawn = asyncio.create_task(asyncio.create_subprocess_exec(sys.executable, '-I',
                                str(Path(__file__).with_name('memory_worker.py')), str(self.directory),
                                self.variant, str(self.threads), str(self.memory_bytes), env=env,
                                start_new_session=True, stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                limit=65536))
                            try:
                                self.process = await asyncio.shield(spawn)
                            except asyncio.CancelledError:
                                self.process = await spawn
                                raise
                    assert self.process is not None  # Shielded spawn completed.
                    assert self.process.stdin is not None and self.process.stdout is not None
                    self.process.stdin.write((json.dumps({'text': text, 'dimensions': self.dimensions}) + '\n').encode())
                    await self.process.stdin.drain()
                    response = json.loads(await self.process.stdout.readline())
                    vector = response['vector']
                    if len(vector) != self.dimensions or not np.isfinite(vector).all():
                        raise ValueError('invalid embedding')
                    self.rss_kib = response['rss_kib']
                    return vector
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self.close()
                raise

    async def close(self):
        if self.process is not None:
            process, self.process = self.process, None
            await Processes.stop(process)


def bounded_passages(source: SourceSuccess) -> Iterator[tuple[int, int, str | None]]:
    """Canonical slices, <=6000 UTF-8 bytes plus bounded title/prefix.

    Byte bounds conservatively bound tokenizer input, including byte fallback;
    the worker checks the actual 8192-token bound too and never truncates. Long
    scopes/windows are split explicitly rather than silently prefix-embedded.
    """
    seen = set()
    text = source['content']
    for start, end, heading in document_passages(source):
        while start < end:
            stop = min(end, start + 6000)
            while len(text[start:stop].encode()) > 6000:
                stop = start + (stop - start) * 3 // 4
            key = start, stop
            if (key not in seen and text[start:stop].strip() and
                    (source.get('document_type') == 'pdf' or _link_density_penalty(text[start:stop]) > 0.1)):
                seen.add(key)
                yield start, stop, heading
            start = stop


def document_input(title: str, passage: str) -> str:
    title = title.encode()[:512].decode(errors='ignore') or 'none'
    return f'title: {title} | text: {passage}'


class MemoryStore:
    def __init__(self, path: Path, model_id: str, *, cap_bytes: int = 1_073_741_824,
                 retention_seconds: float = 0):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.model_id = model_id
        self.cap_bytes = cap_bytes
        self.retention_seconds = retention_seconds
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS documents (
                url TEXT, version TEXT, fetched REAL, source TEXT NOT NULL,
                PRIMARY KEY(url, version));
            CREATE TABLE IF NOT EXISTS passages (
                id INTEGER PRIMARY KEY, url TEXT, version TEXT, start INTEGER, end INTEGER,
                heading TEXT, text TEXT, title TEXT, fetched_at TEXT, model_id TEXT, vector BLOB,
                UNIQUE(url, version, start, end),
                FOREIGN KEY(url, version) REFERENCES documents(url, version) ON DELETE CASCADE);
            CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(text, title, content='passages', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS passage_insert AFTER INSERT ON passages BEGIN
                INSERT INTO passage_fts(rowid,text,title) VALUES(new.id,new.text,new.title); END;
            CREATE TRIGGER IF NOT EXISTS passage_delete AFTER DELETE ON passages BEGIN
                INSERT INTO passage_fts(passage_fts,rowid,text,title) VALUES('delete',old.id,old.text,old.title); END;
        ''')
        self.db.commit()
        self.prune()

    def prune(self):
        with self.lock:
            changed = False
            if self.retention_seconds:
                cursor = self.db.execute('DELETE FROM documents WHERE fetched < ?', (time.time() - self.retention_seconds,))
                changed = bool(cursor.rowcount)
                self.db.commit()
            # VACUUM makes the on-disk bound real rather than merely counting
            # live rows in an ever-growing freelist. This runs off the event loop.
            if changed:
                self._compact()
            while self.path.stat().st_size > self.cap_bytes:
                cursor = self.db.execute('DELETE FROM documents WHERE rowid IN (SELECT rowid FROM documents ORDER BY fetched, rowid LIMIT 1)')
                self.db.commit()
                if not cursor.rowcount:
                    break
                self._compact()

    def _compact(self):
        # FTS deletes append tombstones; VACUUM alone cannot reclaim those
        # logical index segments, even after every document has been evicted.
        self.db.execute("INSERT INTO passage_fts(passage_fts) VALUES ('optimize')")
        self.db.commit()
        self.db.execute('VACUUM')

    def contains(self, source: SourceSuccess, start: int, end: int):
        with self.lock:
            return self.db.execute('SELECT 1 FROM passages WHERE url=? AND version=? AND start=? AND end=? AND model_id=?',
                (source['requested_url'], source['content_version'], start, end, self.model_id)).fetchone() is not None

    def refresh(self, source: SourceSuccess):
        # A fresh acquisition of identical text renews its disclosed fetch date
        # and retention without paying for duplicate embeddings.
        with self.lock, self.db:
            self.db.execute('UPDATE documents SET fetched=?,source=? WHERE url=? AND version=?',
                (datetime.fromisoformat(source['fetched_at']).timestamp(), json.dumps(source, ensure_ascii=False),
                 source['requested_url'], source['content_version']))
            self.db.execute('UPDATE passages SET fetched_at=? WHERE url=? AND version=?',
                (source['fetched_at'], source['requested_url'], source['content_version']))

    def put(self, source: SourceSuccess, start: int, end: int, heading: str | None, vector: list[float]):
        with self.lock, self.db:
            self.db.execute('INSERT INTO documents VALUES (?,?,?,?) ON CONFLICT(url,version) DO UPDATE SET fetched=excluded.fetched, source=excluded.source',
                (source['requested_url'], source['content_version'], datetime.fromisoformat(source['fetched_at']).timestamp(),
                 json.dumps(source, ensure_ascii=False)))
            # Delete explicitly so FTS deletion triggers run even on model changes.
            self.db.execute('DELETE FROM passages WHERE url=? AND version=? AND start=? AND end=?',
                (source['requested_url'], source['content_version'], start, end))
            self.db.execute('INSERT INTO passages(url,version,start,end,heading,text,title,fetched_at,model_id,vector) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (source['requested_url'], source['content_version'], start, end, heading, source['content'][start:end],
                 source['title'], source['fetched_at'], self.model_id, np.asarray(vector, dtype=np.float32).tobytes()))
        self.prune()

    def lookup(self, url: str, content_version: str | None = None) -> SourceSuccess | None:
        with self.lock:
            self.prune()
            row = self.db.execute('SELECT source FROM documents WHERE url=? AND version=?', (url, content_version)).fetchone()
            return json.loads(row[0]) if row else None

    def query(self, query: str, vector: list[float] | None, k: int = 5, exclude: set[str] | None = None) -> list[ToolData]:
        with self.lock:
            self.prune()
            # Bound the ranking pool; cosine scans blobs incrementally, never
            # materializing the entire corpus matrix alongside SQLite's cache.
            excluded = exclude or set()
            pool = max(100, k * 10)
            terms = re.findall(r'\w+', query, re.UNICODE)[:64]
            lexical = []
            if terms:
                expression = ' OR '.join('"' + term + '"' for term in terms)
                lexical = list(islice((row[0] for row in self.db.execute('''SELECT p.id,p.url FROM passage_fts
                    JOIN passages p ON p.id=passage_fts.rowid WHERE passage_fts MATCH ? AND p.model_id=?
                    ORDER BY bm25(passage_fts),p.id''', (expression, self.model_id))
                    if row[1] not in excluded), pool))
            dense = []
            if vector is not None:
                import heapq
                q = np.asarray(vector, dtype=np.float32)
                dense = [pid for _, pid in heapq.nlargest(pool, (
                    (float(np.dot(np.frombuffer(blob, dtype=np.float32), q)), pid)
                    for pid, url, blob in self.db.execute('SELECT id,url,vector FROM passages WHERE model_id=?', (self.model_id,))
                    if url not in excluded))]
            scores: dict[int, float] = {}
            for ranking in (lexical, dense):
                for rank, pid in enumerate(ranking, 1):
                    scores[pid] = scores.get(pid, 0) + 1 / (60 + rank)
            results = []
            for pid in sorted(scores, key=lambda pid: (-scores[pid], pid)):
                row = self.db.execute('SELECT url,version,start,end,heading,text,fetched_at FROM passages WHERE id=?', (pid,)).fetchone()
                url, version, start, end, heading, text, fetched_at = row
                # Retention was swept once at query entry. Do not sweep again
                # mid-ranking and invalidate a later ranked row at an age edge.
                source = json.loads(self.db.execute('SELECT source FROM documents WHERE url=? AND version=?',
                                                    (url, version)).fetchone()[0])
                if source['resolved_url'] in excluded:
                    continue
                reference = {'url': source['resolved_url'], 'start': start, 'end': end,
                             'section': heading, 'text': text, 'evidence_id': evidence_id(source, start, end),
                             **page_references(source, start, end)}
                result = {key: value for key, value in source.items()
                          if key not in ('content', 'link_targets', 'links_truncated')}
                result.update(url=source['resolved_url'], origin='memory', fetched_at=fetched_at,
                              content_mode='highlights', highlights=[reference],
                              retrieval_method='hybrid_fts5_dense_rrf_60' if vector is not None else 'fts5_bm25')
                results.append(result)
                if len(results) == k:
                    break
            return results

    def usage(self):
        with self.lock:
            return {'passages': self.db.execute('SELECT count(*) FROM passages').fetchone()[0],
                    'bytes': self.path.stat().st_size, 'cap_bytes': self.cap_bytes}

    def close(self):
        with self.lock:
            self.db.close()


class Memory:
    def __init__(self, store: MemoryStore, embedder: Embedder, queue_capacity: int = 8):
        self.store = store
        self.embedder = embedder
        self.queue: asyncio.Queue[SourceSuccess] = asyncio.Queue(queue_capacity)
        self.task: asyncio.Task | None = None
        self.drops = 0
        self.failures = 0
        self.closed = False

    def submit(self, source: SourceSuccess):
        if self.closed:
            return
        try:
            self.queue.put_nowait(deepcopy(source))
        except asyncio.QueueFull:
            self.drops += 1
            return
        if self.task is None:
            self.task = asyncio.create_task(self._ingest())

    async def _offload(self, function, *args):
        # asyncio cancellation cannot stop a running SQLite thread. Drain it
        # before shutdown can close the connection or another request reuses it.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _ingest(self):
        while True:
            source = await self.queue.get()
            try:
                await self._offload(self.store.refresh, source)
                passages = await self._offload(lambda: list(bounded_passages(source)))
                for start, end, heading in passages:
                    if await self._offload(self.store.contains, source, start, end):
                        continue
                    vector = await self.embedder.encode(document_input(source['title'], source['content'][start:end]))
                    await self._offload(self.store.put, source, start, end, heading, vector)
            except (OSError, ValueError, KeyError, RuntimeError, TimeoutError, sqlite3.Error):
                self.failures += 1
            finally:
                self.queue.task_done()

    async def query(self, query: str, k: int, exclude: set[str], timeout_seconds: float = 5):
        try:
            async with asyncio.timeout(timeout_seconds):
                vector = await self.embedder.encode(f'task: search result | query: {query}')
                return await self._offload(self.store.query, query, vector, k, exclude)
        except (OSError, ValueError, KeyError, RuntimeError, TimeoutError, sqlite3.Error):
            self.failures += 1
            return []

    def usage(self):
        return {'status': 'enabled', **self.store.usage(), 'ingest_backlog': self.queue.qsize(),
                'ingest_drops': self.drops, 'failures': self.failures}

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.embedder.close()
        # Wait for outstanding to_thread calls before closing SQLite.
        await asyncio.to_thread(self.store.close)
