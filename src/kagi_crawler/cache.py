"""Bounded local canonical versions, independent of browser and evidence selection.

Single service process owns this directory (do not share it between replicas).
JSON record sizes include metadata; eviction is oldest fetched version first,
not query popularity. Expiry is swept on startup and cache use, not by an idle
browser timer. Semantic memory owns independent retention; cache eviction does not evict memory.
"""
import asyncio
from collections.abc import Callable
from typing import cast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from .acquisition import Acquisition
from .types import AcquisitionResult, Source, SourceSuccess


from .safety import contains_secret


class DocumentCache:
    def __init__(self, directory: str | Path, cap_bytes: int = 268_435_456, retention_seconds: float = 604_800) -> None:
        self.directory = Path(directory)
        self.cap_bytes = cap_bytes
        self.retention_seconds = retention_seconds
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.directory.glob('*.tmp'):
            path.unlink()
        self._prune()

    def _path(self, url, version):
        # Hash the complete URL: never drop/reorder meaningful query parameters.
        return self.directory / (hashlib.sha256(url.encode()).hexdigest() + '-' +
                                 hashlib.sha256(version.encode()).hexdigest() + '.json')

    def _records(self):
        records = []
        for path in self.directory.glob('*.json'):
            try:
                stat = path.stat()
                records.append((stat.st_mtime, path, stat.st_size))
            except OSError:
                path.unlink(missing_ok=True)
        return sorted(records, key=lambda r: (r[0], str(r[1])))

    def _prune(self, reserve=0, replacing=None):
        records = self._records()
        # Remove the replaced record first: refreshing identical content must
        # not evict unrelated retained versions just to reserve its own bytes.
        if replacing is not None:
            replacing.unlink(missing_ok=True)
            records = [record for record in records if record[1] != replacing]
        total = sum(size for _, _, size in records)
        for fetched, path, size in records:
            if fetched < time.time() - self.retention_seconds or total + reserve > self.cap_bytes:
                path.unlink(missing_ok=True)
                total -= size

    def usage(self) -> dict[str, int]:
        """Retained record count and serialized bytes (stat only, no reads)."""
        records = self._records()
        return {'entries': len(records), 'bytes': sum(size for _, _, size in records),
                'cap_bytes': self.cap_bytes}

    def lookup(self, url: str, content_version: str | None = None) -> SourceSuccess | None:
        """Return the exact retained version, or latest; never substitute versions.

        Evidence references bind passage offsets to (requested_url, content_version).
        Returned dictionaries are independent copies; selection must not persist
        query metadata or truncate these canonical records.
        """
        self._prune()
        if content_version is not None:
            paths = [self._path(url, content_version)]
        else:
            prefix = hashlib.sha256(url.encode()).hexdigest() + '-'
            paths = [path for _, path, _ in reversed(self._records()) if path.name.startswith(prefix)]
        for path in paths:
            try:
                source = json.loads(path.read_bytes())
                if source['requested_url'] == url and (content_version is None or
                        source['content_version'] == content_version):
                    return source
            except (OSError, ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
        return None

    def store(self, source: SourceSuccess) -> None:
        record = {k: v for k, v in source.items() if k not in ('cache_state', 'stale', 'refresh_error')}
        path = self._path(source['requested_url'], source['content_version'])
        self._write(path, record, source['fetched_at'])

    def _write(self, path: Path, record: dict, fetched_at: str) -> None:
        payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode()
        if len(payload) > self.cap_bytes:
            return
        self._prune(reserve=len(payload), replacing=path)
        temporary = path.with_suffix('.tmp')
        with temporary.open('wb') as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        # mtime is the persisted acquisition time, not read/access time. Index
        # scans need only stat(), never load the whole capped corpus into RAM.
        fetched = datetime.fromisoformat(fetched_at).timestamp()
        os.utime(temporary, (fetched, fetched))
        temporary.replace(path)

    def _summary_path(self, version: str, length: str) -> Path:
        key = json.dumps([version, length, 'summary'])
        return self.directory / ('summary-' + hashlib.sha256(key.encode()).hexdigest() + '.json')

    def summary(self, version: str, length: str) -> dict | None:
        self._prune()
        path = self._summary_path(version, length)
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None

    def store_summary(self, version: str, length: str, summary: dict, fetched_at: str) -> None:
        # Summary records share the canonical cache's byte cap, retention and
        # oldest-acquisition eviction policy, not an unbounded parallel cache.
        self._write(self._summary_path(version, length), summary, fetched_at)


class CachedAcquisition:
    def __init__(self, acquisition: Acquisition, cache: DocumentCache, secrets: tuple[str, ...] = (),
                 on_success: Callable[[SourceSuccess], None] | None = None) -> None:
        self.acquisition = acquisition
        self.cache = cache
        self.secrets = secrets
        self.on_success = on_success

    async def fetch_many(self, urls: list[str], *, freshness: str = 'ordinary', max_age_seconds: float = 3600,
                         scope: Callable[[str], str] | None = None) -> AcquisitionResult:
        # One inherited deadline across concurrent misses; cache hits never join
        # admission. TaskGroup owns cancellation/draining of every miss.
        with self.acquisition.request():
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._fetch(url, freshness, max_age_seconds, scope)) for url in urls]
        return {'sources': [task.result() for task in tasks], 'content_mode': 'full_text'}

    async def _fetch(self, url: str, freshness: str, max_age_seconds: float,
                     scope: Callable[[str], str] | None) -> Source:
        if contains_secret(url, self.secrets):
            return {'error': {'code': 'fetch_failed'}}
        cached = self.cache.lookup(url)
        if cached and contains_secret(cached, self.secrets):
            cached = None
        if freshness == 'cache_only':
            return self._hit(cached, max_age_seconds) if cached else {'requested_url': url, 'error': {'code': 'cache_miss'}}
        if freshness == 'ordinary' and cached and self._age(cached) <= max_age_seconds:
            return self._hit(cached, max_age_seconds)
        # Acquisition owns bounded queueing; never hide it behind an
        # unbounded cache lock. Concurrent misses may acquire twice.
        source: Source = (await self.acquisition.fetch_many([url], scope=scope))['sources'][0]
        if contains_secret(source, self.secrets):
            source = {'error': {'code': 'fetch_failed'}}
        if 'error' not in source:
            self.cache.store(source)
            if self.on_success is not None:
                self.on_success(source)
            source = cast(SourceSuccess, dict(source, cache_state='refresh' if cached else 'miss', stale=False))
        elif cached and freshness != 'fresh_only' and source['error']['code'] not in ('policy_rejected', 'scope_rejected', 'traversal_byte_limit'):
            source = cast(SourceSuccess, dict(cached, cache_state='stale', stale=True, refresh_error=source['error']))
        return source

    @staticmethod
    def _age(source: SourceSuccess) -> float:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(source['fetched_at'])).total_seconds()

    def _hit(self, source: SourceSuccess, max_age: float) -> SourceSuccess:
        stale = self._age(source) > max_age
        return cast(SourceSuccess, dict(source, cache_state='stale' if stale else 'hit', stale=stale))
