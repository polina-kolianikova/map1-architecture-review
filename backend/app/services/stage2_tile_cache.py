"""Bounded cache for immutable published-mask tile responses."""
import hashlib
import json
import os
import stat
from pathlib import Path
from collections import OrderedDict
from threading import Event, RLock
from time import monotonic


class TileCache:
    def __init__(self, max_bytes=32 * 1024 * 1024, max_entries=128, ttl=300, clock=monotonic, published_dir=None, trusted_uid=0):
        if min(max_bytes, max_entries, ttl) <= 0:
            raise ValueError("Tile cache limits must be positive")
        self.published_dir = Path(published_dir) if published_dir else None
        self.trusted_uid = trusted_uid
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.ttl = ttl
        self.clock = clock
        self._items = OrderedDict()
        self._pending = {}
        self._bytes = 0
        self._lock = RLock()

    def get_or_load(self, key, loader):
        while True:
            with self._lock:
                now = self.clock()
                for expired in [k for k, (until, _) in self._items.items() if until <= now]:
                    self._bytes -= len(self._items.pop(expired)[1])
                if key in self._items:
                    entry = self._items.pop(key)
                    self._items[key] = entry
                    return entry[1]
                pending = self._pending.get(key)
                if pending is None:
                    pending = self._pending[key] = Event()
                    break
            if not pending.wait(60):
                raise TimeoutError("Published mask tile is still being prepared")
        try:
            value = self._read_published(key)
            if value is None:
                value = bytes(loader() or b"")
            with self._lock:
                if len(value) <= self.max_bytes:
                    while self._items and (
                        len(self._items) >= self.max_entries or self._bytes + len(value) > self.max_bytes
                    ):
                        _, (_, removed) = self._items.popitem(last=False)
                        self._bytes -= len(removed)
                    self._items[key] = (self.clock() + self.ttl, value)
                    self._bytes += len(value)
            return value
        finally:
            with self._lock:
                self._pending.pop(key).set()

    def _read_published(self, key):
        root = self.published_dir
        if root is None:
            return None
        directory = None
        try:
            for path in (root.parent, root):
                metadata = path.lstat()
                if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != self.trusted_uid
                        or metadata.st_mode & 0o022):
                    return None
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            digest = tile_key_digest(key)
            def read(name, limit):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as handle:
                    metadata = os.fstat(handle.fileno())
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != self.trusted_uid
                            or metadata.st_mode & 0o022 or metadata.st_size > limit):
                        raise ValueError('Untrusted published tile')
                    value = handle.read(limit + 1)
                    if len(value) > limit:
                        raise ValueError('Published tile size exceeded')
                    return value
            receipt = json.loads(read(digest + '.json', 16384))
            if receipt['schema'] != 1 or receipt['key_sha256'] != digest or receipt['encoding'] != 'identity':
                return None
            raw = read(digest + '.mvt', 64 * 1024 * 1024)
            if receipt['size'] != len(raw) or receipt['content_sha256'] != hashlib.sha256(raw).hexdigest():
                return None
            return raw
        except (OSError, ValueError, KeyError, TypeError):
            return None
        finally:
            if directory is not None:
                os.close(directory)


def tile_key_digest(key):
    return hashlib.sha256(json.dumps(key, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def mask_revision(metadata):
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def mask_tile_key(revision, region_id, mask_version, grid_version, srid, z, x, y):
    return ('published-components-mvt-4096-v1', revision, region_id, mask_version, grid_version, srid, z, x, y)
