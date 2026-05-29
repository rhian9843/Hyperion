import struct
from pathlib import Path

from .errors import TransactionError
from .constants import PAGE_SIZE
from .checksum import stamp_page, verify_page
from .wal import WAL

try:
    import errno as _errno
    import fcntl as _fcntl

    def _flock(fd: int, how: int) -> None:
        try:
            _fcntl.flock(fd, how)
        except OSError:
            pass  # some filesystems (NFS, tmpfs on some platforms) don't support flock

    def _flock_try_ex(fd: int) -> bool:
        """Non-blocking LOCK_EX attempt. Returns True if acquired (or unsupported)."""
        try:
            _fcntl.flock(fd, 2 | 4)  # LOCK_EX | LOCK_NB
            return True
        except OSError as e:
            if e.errno in (_errno.EAGAIN, _errno.EWOULDBLOCK):
                return False  # another connection holds a lock — skip WAL replay
            return True  # filesystem doesn't support flock — proceed as if acquired

except ImportError:
    def _flock(fd: int, how: int) -> None:  # type: ignore[misc]
        pass  # Windows or other platform without fcntl

    def _flock_try_ex(fd: int) -> bool:  # type: ignore[misc]
        return True


class Pager:
    def __init__(self, path: Path, *, readonly: bool = False):
        wal_path = path.with_suffix(".wal")
        if readonly:
            self._file = open(path, "rb")
            _flock(self._file.fileno(), 1)   # LOCK_SH — read-only, no WAL replay
        else:
            self._file = open(path, "r+b" if path.exists() else "w+b")
            # Try non-blocking LOCK_EX for WAL crash recovery.  If another connection
            # is already open (holding LOCK_SH) we skip recovery — the live connection
            # will handle it on its own close/checkpoint.
            if _flock_try_ex(self._file.fileno()):
                WAL.replay_if_exists(wal_path, self._file)
            _flock(self._file.fileno(), 1)   # LOCK_SH — hold while open
        self._path    = path
        self._cache:   dict[int, bytearray] = {}  # committed pages (stable snapshot)
        self._working: dict[int, bytearray] = {}  # in-transaction copy-on-write pages
        self._dirty:   set[int] = set()
        self._wal:     WAL | None = None          # opened lazily on first begin()
        self._in_txn:  bool = False
        self._wal_txn_offset: int = 0             # WAL offset at transaction start

    def _load(self, num: int) -> bytearray:
        if num not in self._cache:
            page = bytearray(PAGE_SIZE)
            self._file.seek(num * PAGE_SIZE)
            chunk = self._file.read(PAGE_SIZE)
            page[: len(chunk)] = chunk
            verify_page(page, num)
            self._cache[num] = page
        return self._cache[num]

    def read_page(self, num: int) -> bytearray:
        """Return the current view of a page without marking it dirty.

        During a write transaction returns the working (in-progress) copy so
        the writer can read its own writes.  Outside a transaction — including
        concurrent streaming readers on the same connection — returns the
        committed snapshot from _cache, preventing dirty reads.
        """
        if self._in_txn and num in self._working:
            return self._working[num]
        return self._load(num)

    def get_page(self, num: int) -> bytearray:
        """Return a writable page and mark it dirty.

        During a transaction the page is copy-on-write'd into _working so
        that _cache always holds the pre-transaction committed state.
        """
        if self._in_txn:
            if num not in self._working:
                self._working[num] = bytearray(self._load(num))
            self._dirty.add(num)
            return self._working[num]
        page = self._load(num)
        self._dirty.add(num)
        return page

    def flush(self, num: int) -> None:
        self._dirty.add(num)

    def begin(self) -> None:
        if self._in_txn:
            raise TransactionError("Transaction already active")
        _flock(self._file.fileno(), 2)   # LOCK_EX — upgrade from shared
        self._working.clear()
        self._dirty.clear()
        if self._wal is None:
            self._wal = WAL(self._path.with_suffix(".wal"))
        self._wal_txn_offset = self._wal.begin_offset()
        self._in_txn = True

    def commit(self) -> None:
        if not self._in_txn:
            raise TransactionError("No active transaction")
        assert self._wal is not None
        for page in self._working.values():
            stamp_page(page)
        self._wal.commit_txn(self._working)
        self._cache.update(self._working)
        self._working.clear()
        self._dirty.clear()
        self._in_txn = False
        # Always checkpoint before releasing LOCK_EX so the main file is fully
        # current when LOCK_EX downgrades to LOCK_SH.  Any connection that
        # subsequently acquires LOCK_SH will read an up-to-date main file and
        # can safely skip WAL replay.  (WAL still provides crash durability for
        # the commit_txn fsync that preceded this.)
        self._wal.checkpoint(self._file)
        _flock(self._file.fileno(), 1)   # LOCK_SH — downgrade after write

    def rollback(self) -> None:
        if not self._in_txn:
            raise TransactionError("No active transaction")
        assert self._wal is not None
        self._wal.rollback_txn(self._wal_txn_offset)
        self._working.clear()
        self._dirty.clear()
        self._in_txn = False
        _flock(self._file.fileno(), 1)   # LOCK_SH — downgrade after abort

    def close(self) -> None:
        if self._in_txn and self._wal is not None:
            self._wal.rollback_txn(self._wal_txn_offset)
            self._in_txn = False
        if self._wal is not None:
            # Final checkpoint: flush accumulated WAL frames to main file.
            _flock(self._file.fileno(), 2)   # LOCK_EX briefly for checkpoint
            self._wal.checkpoint(self._file)
            self._wal.close()
            self._wal = None
            # WAL file was truncated to header by checkpoint; remove it.
            wal_path = self._path.with_suffix(".wal")
            wal_path.unlink(missing_ok=True)
        self._file.flush()
        _flock(self._file.fileno(), 8)   # LOCK_UN
        self._file.close()


class MemoryPager:
    """Pager backed by an in-memory dict — no file I/O, no WAL, no locking.

    Uses copy-on-write snapshot isolation: _cache holds the stable committed
    state; _working holds pages modified by the current write transaction.
    Readers always see _cache (no dirty reads); the writer sees its own
    writes via _working.  Rollback is O(1) — just discard _working.
    """

    def __init__(self) -> None:
        self._path    = Path(":memory:")
        self._cache:   dict[int, bytearray] = {}  # committed pages
        self._working: dict[int, bytearray] = {}  # in-transaction CoW pages
        self._dirty:   set[int] = set()
        self._wal      = None  # API compatibility with Pager
        self._in_txn:  bool = False

    def _load(self, num: int) -> bytearray:
        if num not in self._cache:
            self._cache[num] = bytearray(PAGE_SIZE)
        return self._cache[num]

    def read_page(self, num: int) -> bytearray:
        """Return the current view of a page (read-only path).

        Returns the working copy when the caller is a writer reading its own
        writes; otherwise returns the committed snapshot so that concurrent
        streaming readers never see uncommitted data.
        """
        if self._in_txn and num in self._working:
            return self._working[num]
        return self._load(num)

    def get_page(self, num: int) -> bytearray:
        """Return a writable page and mark it dirty.

        During a transaction the page is copy-on-write'd into _working so
        that _cache always holds the pre-transaction committed state.
        """
        if self._in_txn:
            if num not in self._working:
                self._working[num] = bytearray(self._load(num))
            self._dirty.add(num)
            return self._working[num]
        page = self._load(num)
        self._dirty.add(num)
        return page

    def flush(self, num: int) -> None:
        self._dirty.add(num)

    def begin(self) -> None:
        if self._in_txn:
            raise TransactionError("Transaction already active")
        self._working.clear()
        self._dirty.clear()
        self._in_txn = True

    def commit(self) -> None:
        if not self._in_txn:
            raise TransactionError("No active transaction")
        self._cache.update(self._working)
        self._working.clear()
        self._dirty.clear()
        self._in_txn = False

    def rollback(self) -> None:
        if not self._in_txn:
            raise TransactionError("No active transaction")
        # Discard working pages — _cache is untouched, so no restore needed.
        self._working.clear()
        self._dirty.clear()
        self._in_txn = False

    def close(self) -> None:
        pass
