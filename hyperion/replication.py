"""Logical replication — subscription worker and change-application logic.

The subscription worker is a daemon thread that:
  1. Polls the primary's HTTP endpoint:
       GET /replication/changes?publication=<pub>&since=<last_lsn>
  2. Applies each returned change (INSERT/UPDATE/DELETE) to the local replica db.
  3. Persists last_lsn so the next poll only fetches new entries.

Apply strategy
--------------
INSERT  →  INSERT OR REPLACE (idempotent if row already exists)
UPDATE  →  INSERT OR REPLACE with new row (requires PK on replica table)
DELETE  →  DELETE FROM table WHERE <pk_col> = <pk_val>

Tables with no primary key cannot use DELETE replication safely; those entries
are skipped with a warning.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

_POLL_INTERVAL = 1.0   # seconds between polls
_CONNECT_TIMEOUT = 5   # seconds for HTTP connect/read


# ── Apply helpers ─────────────────────────────────────────────────────────────

def _sql_literal(v: Any) -> str:
    """Convert a Python value to a SQL literal string."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _pk_cols_for(db: "Database", table: str) -> list[str]:
    """Return primary-key column names for a table, or []."""
    if table not in db.tables:
        return []
    schema = db.tables[table].schema
    pk = [c.name for c in schema.columns if c.primary_key]
    if not pk and schema.primary_key_columns:
        pk = list(schema.primary_key_columns)
    return pk


def apply_change(db: "Database", entry: dict) -> None:
    """Apply a single changelog entry dict to the replica database.

    Raises nothing — silently skips entries for tables that don't exist on
    the replica yet, or DELETEs on tables with no primary key.
    """
    table = entry.get("table")
    op    = entry.get("op")
    row   = entry.get("row")

    if not table or not op:
        return
    if table not in db.tables:
        return

    if op == "INSERT" or op == "UPDATE":
        if not row:
            return
        cols    = list(row.keys())
        cols_s  = ", ".join(f'"{c}"' for c in cols)
        vals_s  = ", ".join(_sql_literal(row[c]) for c in cols)
        db.execute(f'INSERT OR REPLACE INTO "{table}" ({cols_s}) VALUES ({vals_s})')

    elif op == "DELETE":
        if not row:
            return
        pk = _pk_cols_for(db, table)
        if not pk:
            return  # no PK — can't identify row safely
        where_parts = [f'"{pc}" = {_sql_literal(row.get(pc))}' for pc in pk]
        db.execute(f'DELETE FROM "{table}" WHERE {" AND ".join(where_parts)}')


# ── HTTP fetch helper ─────────────────────────────────────────────────────────

def _fetch_changes(connection: str, publication: str,
                   since_lsn: int) -> list[dict]:
    """Fetch changelog entries from the primary's HTTP endpoint."""
    params = urllib.parse.urlencode({"publication": publication, "since": since_lsn})
    url    = f"{connection.rstrip('/')}/replication/changes?{params}"
    req    = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return data.get("changes", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            OSError, TimeoutError):
        return []


# ── Background subscription worker ───────────────────────────────────────────

class SubscriptionWorker:
    """Background thread that polls a primary and applies changes locally.

    Parameters
    ----------
    db            : The local replica database.
    sub_name      : Subscription name (must exist in db._catalog.subscriptions).
    poll_interval : Seconds between polls (default 1 s).
    """

    def __init__(self, db: "Database", sub_name: str,
                 poll_interval: float = _POLL_INTERVAL) -> None:
        self._db           = db
        self._sub_name     = sub_name
        self._poll_interval = poll_interval
        self._stop         = threading.Event()
        self._thread       = threading.Thread(
            target=self._run, name=f"hyperion-sub-{sub_name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                pass  # worker must never crash
            self._stop.wait(self._poll_interval)

    def _poll_once(self) -> None:
        db       = self._db
        cat      = db._catalog
        sub      = cat.subscriptions.get(self._sub_name)
        if sub is None:
            self._stop.set()
            return

        changes = _fetch_changes(sub.connection, sub.publication, sub.last_lsn)
        if not changes:
            return

        with db._lock.write():
            db._pager.begin()
            db._txn_depth = 1
            try:
                max_lsn = sub.last_lsn
                for entry in changes:
                    try:
                        apply_change(db, entry)
                    except Exception:
                        pass
                    max_lsn = max(max_lsn, entry.get("lsn", max_lsn))
                sub.last_lsn = max_lsn
                db._flush_catalog()
                db._pager.commit()
            except Exception:
                db._pager.rollback()
                db._reload_catalog()
            finally:
                db._txn_depth = 0
