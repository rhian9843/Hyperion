"""Physical replication — page-level WAL streaming from primary to replica.

Architecture
------------
Primary side (no configuration needed):
  GET /replication/physical/snapshot
    Full copy of db file + WAL under a read lock, encoded as base64 JSON.
    Used for initial sync or when the replica has fallen behind a checkpoint.

  GET /replication/physical/changes?since=<lsn>
    Pages modified since the given catalog.lsn.  Returns snapshot_required=true
    if the replica's lsn predates the last primary checkpoint (pages no longer
    in _phys_dirty — already merged into the db file by checkpoint).

Replica side:
  PhysicalReplicationWorker — daemon thread, polls primary every 500 ms,
  applies page bytes directly into the replica pager.  Read-only mode is
  enabled while the worker is active; STOP SLAVE re-enables writes (promotion).

State persistence
-----------------
Physical subscription configuration lives in a `<db>.phys_state` JSON file
next to the database, NOT in the catalog.  This is necessary because physical
replication overwrites catalog pages with primary catalog pages on every sync,
which would wipe subscription metadata stored in the catalog.

SQL interface
-------------
  CREATE PHYSICAL SUBSCRIPTION name CONNECTION 'http://primary:port'
  DROP PHYSICAL SUBSCRIPTION [IF EXISTS] name
  START SLAVE [name]    -- start worker, enables read-only
  STOP SLAVE  [name]    -- stop worker, re-enables writes (promotion)
  SHOW MASTER STATUS    -- binlog_pos, db_size, wal_size
  SHOW SLAVE STATUS     -- per-subscription: last_lsn, lag, status
  SHOW BINLOG           -- current physical page change log
"""
from __future__ import annotations

import base64
import json
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database

_POLL_INTERVAL   = 0.5   # seconds between polls
_CONNECT_TIMEOUT = 5     # HTTP connect/read timeout


# ── Metadata ──────────────────────────────────────────────────────────────────

@dataclass
class PhysicalSubscriptionMeta:
    name:       str
    connection: str    # 'http://host:port' of the primary
    last_lsn:   int  = 0
    auto_start: bool = True   # restart worker when database is reopened


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _fetch_snapshot(connection: str) -> dict:
    return _http_get_json(f"{connection.rstrip('/')}/replication/physical/snapshot")


def _fetch_changes(connection: str, since_lsn: int) -> dict:
    params = urllib.parse.urlencode({"since": since_lsn})
    return _http_get_json(
        f"{connection.rstrip('/')}/replication/physical/changes?{params}"
    )


# ── Page application ──────────────────────────────────────────────────────────

def apply_snapshot(db: "Database", snapshot: dict, sub_name: str) -> None:
    """Replace the replica's database content with a full primary snapshot.

    Decodes the snapshot's db_file pages and WAL committed PAGE frames, then
    applies them all as a single pager transaction (WAL frames take precedence
    over db_file bytes since they represent more recent state).
    """
    from .constants import PAGE_SIZE
    from .pager import MemoryPager
    from .wal import WAL

    if isinstance(db._pager, MemoryPager):
        return

    db_bytes  = base64.b64decode(snapshot["db_data"])
    wal_b64   = snapshot.get("wal_data") or ""
    wal_bytes = base64.b64decode(wal_b64) if wal_b64 else b""
    lsn       = snapshot["lsn"]

    # Build page_map: db_file pages first, then WAL committed pages (more recent).
    page_map: dict[int, bytes] = {}
    for offset in range(0, len(db_bytes), PAGE_SIZE):
        chunk = db_bytes[offset: offset + PAGE_SIZE]
        if len(chunk) == PAGE_SIZE:
            page_map[offset // PAGE_SIZE] = chunk

    if len(wal_bytes) > WAL.HDR_SIZE:
        pos     = WAL.HDR_SIZE
        pending: list[tuple[int, bytes]] = []
        while pos + WAL.FRAME_SZ <= len(wal_bytes):
            frame = wal_bytes[pos: pos + WAL.FRAME_SZ]
            pn    = struct.unpack_from("<I", frame)[0]
            if pn == WAL.COMMIT_PN:
                for ppn, pdata in pending:
                    page_map[ppn] = pdata
                pending.clear()
            elif pn not in (WAL.LOGICAL_PN,):
                pending.append((pn, bytes(frame[4:])))
            pos += WAL.FRAME_SZ

    _apply_pages_internal(db, list(page_map.items()), lsn, sub_name)


def apply_incremental(db: "Database", changes: dict, sub_name: str) -> None:
    """Apply incremental page changes returned by the primary's changes endpoint."""
    lsn   = changes["lsn"]
    pages = changes.get("pages") or []
    if not pages:
        _update_last_lsn(db, lsn, sub_name)
        return
    pairs = [(p["page_num"], base64.b64decode(p["data"])) for p in pages]
    _apply_pages_internal(db, pairs, lsn, sub_name)


def _apply_pages_internal(db: "Database",
                           pages: list[tuple[int, bytes]],
                           lsn: int,
                           sub_name: str) -> None:
    """Write (page_num, page_bytes) pairs to the replica pager as one transaction.

    catalog_lsn=0 is passed to pager.commit so these replica-applied pages are
    NOT recorded in _phys_dirty — the replica is not a primary serving others.
    After commit, _reload_catalog() picks up the primary's schema from the newly
    written catalog pages.  Physical subscription state is preserved in the
    separate .phys_state file.
    """
    with db._lock.write():
        if db._txn_depth != 0:
            return   # another transaction is active; skip this cycle
        db._pager.begin()
        db._txn_depth = 1
        try:
            for pn, data in pages:
                page = db._pager.get_page(pn)
                page[: len(data)] = data
            # catalog_lsn=0 → skip _phys_dirty recording (replica, not primary)
            db._pager.commit(min_consumed_lsn=db._min_consumed_lsn(), catalog_lsn=0)
            db._reload_catalog()
            sub = db._phys_subs.get(sub_name)
            if sub is not None:
                sub.last_lsn = lsn
                db._save_phys_subs()
        except Exception:
            db._pager.rollback()
            db._reload_catalog()
            raise
        finally:
            db._txn_depth = 0


def _update_last_lsn(db: "Database", lsn: int, sub_name: str) -> None:
    sub = db._phys_subs.get(sub_name)
    if sub is not None and sub.last_lsn != lsn:
        sub.last_lsn = lsn
        db._save_phys_subs()


# ── Background worker ─────────────────────────────────────────────────────────

class PhysicalReplicationWorker:
    """Daemon thread that keeps a replica in sync with its primary.

    Poll cycle (every POLL_INTERVAL):
      1. last_lsn == 0 → full snapshot sync.
      2. Otherwise → request incremental page changes since last_lsn.
         If primary responds snapshot_required → fall back to full snapshot.

    Stopping the worker (STOP SLAVE) re-enables writes on the replica,
    promoting it to a standalone primary-capable database.
    """

    def __init__(self, db: "Database", sub_name: str,
                 poll_interval: float = _POLL_INTERVAL) -> None:
        self._db             = db
        self._sub_name       = sub_name
        self._poll_interval  = poll_interval
        self._stop           = threading.Event()
        self._thread         = threading.Thread(
            target=self._run,
            name=f"hyperion-phys-{sub_name}",
            daemon=True,
        )
        self._last_sync_ts: float = 0.0
        self._last_error:   str   = ""
        self.status:        str   = "Starting"

    def start(self) -> None:
        self._db._readonly = True
        self._thread.start()
        self.status = "Running"

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)
        self.status = "Stopped"
        self._db._readonly = False   # promote: re-enable writes

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sync_once()
                self._last_error = ""
                self.status = "Running"
            except Exception as exc:
                self._last_error = str(exc)
                self.status = "Retrying"
            self._stop.wait(self._poll_interval)

    def _sync_once(self) -> None:
        sub = self._db._phys_subs.get(self._sub_name)
        if sub is None:
            self._stop.set()
            return

        if sub.last_lsn == 0:
            snapshot = _fetch_snapshot(sub.connection)
            apply_snapshot(self._db, snapshot, self._sub_name)
        else:
            changes = _fetch_changes(sub.connection, sub.last_lsn)
            if changes.get("snapshot_required"):
                snapshot = _fetch_snapshot(sub.connection)
                apply_snapshot(self._db, snapshot, self._sub_name)
            else:
                apply_incremental(self._db, changes, self._sub_name)

        self._last_sync_ts = time.time()
