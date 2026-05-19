import struct
from pathlib import Path

from .constants import PAGE_SIZE
from .wal import WAL


class Pager:
    def __init__(self, path: Path):
        wal_path = path.with_suffix(".wal")
        self._file = open(path, "r+b" if path.exists() else "w+b")
        WAL.replay_if_exists(wal_path, self._file)
        self._path  = path
        self._cache: dict[int, bytearray] = {}
        self._dirty: set[int] = set()
        self._wal:   WAL | None = None

    def get_page(self, num: int) -> bytearray:
        if num not in self._cache:
            page = bytearray(PAGE_SIZE)
            self._file.seek(num * PAGE_SIZE)
            chunk = self._file.read(PAGE_SIZE)
            page[: len(chunk)] = chunk
            self._cache[num] = page
        self._dirty.add(num)
        return self._cache[num]

    def flush(self, num: int) -> None:
        self._dirty.add(num)

    def begin(self) -> None:
        if self._wal is not None:
            raise RuntimeError("Transaction already active")
        self._dirty.clear()
        self._wal = WAL(self._path.with_suffix(".wal"))

    def commit(self) -> None:
        if self._wal is None:
            raise RuntimeError("No active transaction")
        dirty = {n: self._cache[n] for n in self._dirty if n in self._cache}
        self._wal.commit(dirty, self._file)
        self._wal = None
        self._dirty.clear()

    def rollback(self) -> None:
        if self._wal is None:
            raise RuntimeError("No active transaction")
        self._wal.rollback()
        self._wal = None
        for n in self._dirty:
            self._cache.pop(n, None)
        self._dirty.clear()

    def close(self) -> None:
        if self._wal is not None:
            self._wal.rollback()
            self._wal = None
        self._file.flush()
        self._file.close()
