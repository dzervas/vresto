"""Bounded local canonical versions, independent of browser and evidence selection.

Single service process owns this directory (do not share it between replicas).
JSON record sizes include metadata; eviction is oldest fetched version first,
not query popularity. Expiry is swept on startup and cache use, not by an idle
browser timer. No separate index/evidence store can outlive its document.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time


class DocumentCache:
    def __init__(self, directory, cap_bytes=268_435_456, retention_seconds=604_800):
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

    def lookup(self, url, content_version=None):
        """Return the exact retained version, or latest; never substitute versions.

        .6/.7 can bind passage offsets to (requested_url, content_version).
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

    def store(self, source):
        source = {k: v for k, v in source.items() if k not in ('cache_state', 'stale', 'refresh_error')}
        payload = json.dumps(source, ensure_ascii=False, separators=(',', ':')).encode()
        if len(payload) > self.cap_bytes:
            return  # Acquisition succeeds even if a single document cannot fit.
        path = self._path(source['requested_url'], source['content_version'])
        self._prune(reserve=len(payload), replacing=path)
        temporary = path.with_suffix('.tmp')
        with temporary.open('wb') as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        # mtime is the persisted acquisition time, not read/access time. Index
        # scans need only stat(), never load the whole capped corpus into RAM.
        fetched = datetime.fromisoformat(source['fetched_at']).timestamp()
        os.utime(temporary, (fetched, fetched))
        temporary.replace(path)


class CachedAcquisition:
    def __init__(self, acquisition, cache, secrets=()):
        self.acquisition = acquisition
        self.cache = cache
        self.secrets = secrets

    async def fetch_many(self, urls, *, freshness='ordinary', max_age_seconds=3600):
        sources = []
        for url in urls:
            cached = self.cache.lookup(url)
            if freshness == 'cache_only':
                sources.append(self._hit(cached, max_age_seconds) if cached else
                               {'requested_url': url, 'error': {'code': 'cache_miss'}})
                continue
            if freshness == 'ordinary' and cached and self._age(cached) <= max_age_seconds:
                sources.append(self._hit(cached, max_age_seconds))
                continue
            # Acquisition owns bounded queueing; never hide it behind an
            # unbounded cache lock. Concurrent misses may acquire twice.
            source = (await self.acquisition.fetch_many([url]))['sources'][0]
            if any(secret in value for value in source.values() if isinstance(value, str) for secret in self.secrets):
                source = {'error': {'code': 'fetch_failed'}}
            if 'error' not in source:
                self.cache.store(source)
                source = dict(source, cache_state='refresh' if cached else 'miss', stale=False)
            elif cached and freshness != 'fresh_only' and source['error']['code'] != 'policy_rejected':
                source = dict(cached, cache_state='stale', stale=True, refresh_error=source['error'])
            sources.append(source)
        return {'sources': sources, 'content_mode': 'full_text'}

    @staticmethod
    def _age(source):
        return (datetime.now(timezone.utc) - datetime.fromisoformat(source['fetched_at'])).total_seconds()

    def _hit(self, source, max_age):
        stale = self._age(source) > max_age
        return dict(source, cache_state='stale' if stale else 'hit', stale=stale)
