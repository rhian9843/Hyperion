"""Changelog objects for logical replication.

On-disk databases use WALBackedChangelog, which reads retained LOGICAL frames
directly from WAL._committed_logical (an in-memory mirror of the WAL file).
No separate archive file is created or maintained.

In-memory databases use InMemoryChangelog for unit tests and HTTP tests.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


@dataclass
class ChangelogEntry:
    lsn:        int
    table:      str
    op:         str              # "INSERT" | "UPDATE" | "DELETE"
    row:        dict | None      # new row (INSERT/UPDATE) or deleted row (DELETE)
    row_before: dict | None      # pre-update row (UPDATE only)
    ts:         float

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "lsn": self.lsn, "table": self.table,
            "op": self.op, "ts": self.ts,
        }
        if self.row is not None:
            d["row"] = self.row
        if self.row_before is not None:
            d["row_before"] = self.row_before
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ChangelogEntry":
        return cls(
            lsn=d["lsn"], table=d["table"], op=d["op"],
            row=d.get("row"), row_before=d.get("row_before"),
            ts=d.get("ts", 0.0),
        )


class WALBackedChangelog:
    """Reads logical changelog entries from the WAL's in-memory retained buffer.

    WAL._committed_logical holds ALL unconsumed logical entries: entries that
    survived the last checkpoint (lsn > min_consumed_lsn at checkpoint time) plus
    entries committed since the last checkpoint.  This is the single source of
    truth — no separate .changelog archive file exists.

    Thread-safety: reads are protected by WAL._committed_lock; writing is done
    only inside commit_txn() and checkpoint(), both of which hold the same lock.
    """

    def __init__(self, pager) -> None:
        self._pager = pager

    def _all_entries(self) -> list[ChangelogEntry]:
        w = self._pager._wal
        if w is None:
            return []
        with w._committed_lock:
            return [ChangelogEntry.from_dict(d) for d in list(w._committed_logical)]

    def read_since(self, since_lsn: int) -> list[ChangelogEntry]:
        return [e for e in self._all_entries() if e.lsn > since_lsn]

    def latest_lsn(self) -> int:
        entries = self._all_entries()
        return entries[-1].lsn if entries else 0

    def read_for_publication(self, tables: list[str],
                              since_lsn: int) -> list[ChangelogEntry]:
        entries = self.read_since(since_lsn)
        if not tables:
            return entries
        ts = set(tables)
        return [e for e in entries if e.table in ts]


class InMemoryChangelog:
    """In-memory changelog for :memory: databases and unit tests."""

    def __init__(self) -> None:
        self._entries: list[ChangelogEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: ChangelogEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def read_since(self, since_lsn: int) -> list[ChangelogEntry]:
        with self._lock:
            return [e for e in self._entries if e.lsn > since_lsn]

    def latest_lsn(self) -> int:
        with self._lock:
            return self._entries[-1].lsn if self._entries else 0

    def read_for_publication(self, tables: list[str],
                              since_lsn: int) -> list[ChangelogEntry]:
        entries = self.read_since(since_lsn)
        if not tables:
            return entries
        table_set = set(tables)
        return [e for e in entries if e.table in table_set]
