import struct
from pathlib import Path

from .constants import PAGE_SIZE
from .wal import WAL

try:
    import fcntl as _fcntl
    def _flock(fd: int, how: int) -> None:
        try:
            _fcntl.flock(fd, how)
        except OSError:
            pass  # some filesystems (NFS, tmpfs on some platforms) don't support flock
except ImportError:
    def _flock(fd: int, how: int) -> None:  # type: ignore[misc]
        pass  # Windows or other platform without fcntl


class Pager:
    def __init__(self, path: Path):
        wal_path = path.with_suffix(".wal")
        self._file = open(path, "r+b" if path.exists() else "w+b")
        # Acquire exclusive lock briefly for WAL crash recovery, then downgrade to shared
        _flock(self._file.fileno(), 2)   # LOCK_EX
        WAL.replay_if_exists(wal_path, self._file)
        _flock(self._file.fileno(), 1)   # LOCK_SH — hold while open
        self._path  = path
        self._cache: dict[int, bytearray] = {}
        self._dirty: set[int] = set()
        self._wal:   WAL | None = None

    def _load(self, num: int) -> bytearray:
        if num not in self._cache:
            page = bytearray(PAGE_SIZE)
            self._file.seek(num * PAGE_SIZE)
            chunk = self._file.read(PAGE_SIZE)
            page[: len(chunk)] = chunk
            self._cache[num] = page
        return self._cache[num]

    def read_page(self, num: int) -> bytearray:
        """Return a cached page without marking it dirty (read-only path)."""
        return self._load(num)

    def get_page(self, num: int) -> bytearray:
        """Return a cached page and mark it dirty (write path)."""
        page = self._load(num)
        self._dirty.add(num)
        return page

    def flush(self, num: int) -> None:
        self._dirty.add(num)

    def begin(self) -> None:
        if self._wal is not None:
            raise RuntimeError("Transaction already active")
        _flock(self._file.fileno(), 2)   # LOCK_EX — upgrade from shared
        self._dirty.clear()
        self._wal = WAL(self._path.with_suffix(".wal"))

    def commit(self) -> None:
        if self._wal is None:
            raise RuntimeError("No active transaction")
        dirty = {n: self._cache[n] for n in self._dirty if n in self._cache}
        self._wal.commit(dirty, self._file)
        self._wal = None
        self._dirty.clear()
        _flock(self._file.fileno(), 1)   # LOCK_SH — downgrade after write

    def rollback(self) -> None:
        if self._wal is None:
            raise RuntimeError("No active transaction")
        self._wal.rollback()
        self._wal = None
        for n in self._dirty:
            self._cache.pop(n, None)
        self._dirty.clear()
        _flock(self._file.fileno(), 1)   # LOCK_SH — downgrade after abort

    def close(self) -> None:
        if self._wal is not None:
            self._wal.rollback()
            self._wal = None
        self._file.flush()
        _flock(self._file.fileno(), 8)   # LOCK_UN
        self._file.close()


class MemoryPager:
    """Pager backed by an in-memory dict — no file I/O, no WAL, no locking."""

    def __init__(self) -> None:
        self._path  = Path(":memory:")
        self._cache: dict[int, bytearray] = {}
        self._dirty: set[int] = set()
        self._wal   = None
        self._snap: dict[int, bytearray] | None = None

    def _load(self, num: int) -> bytearray:
        if num not in self._cache:
            self._cache[num] = bytearray(PAGE_SIZE)
        return self._cache[num]

    def read_page(self, num: int) -> bytearray:
        return self._load(num)

    def get_page(self, num: int) -> bytearray:
        page = self._load(num)
        self._dirty.add(num)
        return page

    def flush(self, num: int) -> None:
        self._dirty.add(num)

    def begin(self) -> None:
        if self._snap is not None:
            raise RuntimeError("Transaction already active")
        # Snapshot current pages so rollback can restore them
        self._snap = {n: bytearray(p) for n, p in self._cache.items()}
        self._dirty.clear()

    def commit(self) -> None:
        if self._snap is None:
            raise RuntimeError("No active transaction")
        self._snap = None
        self._dirty.clear()

    def rollback(self) -> None:
        if self._snap is None:
            raise RuntimeError("No active transaction")
        # Remove pages added during the transaction
        for n in set(self._cache) - set(self._snap):
            del self._cache[n]
        # Restore pages that existed before the transaction
        for n, data in self._snap.items():
            self._cache[n] = data
        self._snap = None
        self._dirty.clear()

    def close(self) -> None:
        pass
