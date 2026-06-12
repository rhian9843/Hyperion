"""Tests for physical replication — page-level streaming from primary to replica."""
from __future__ import annotations

import base64
import json
import struct
import threading
import time
from pathlib import Path

import pytest

from hyperion.database import Database
from hyperion.physical_replication import (
    PhysicalSubscriptionMeta,
    PhysicalReplicationWorker,
    apply_snapshot,
    apply_incremental,
    _fetch_snapshot,
    _fetch_changes,
)
from hyperion.http_server import HTTPServerMode
from hyperion.pager import MemoryPager


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_primary(tmp_path: Path, name: str = "primary.hdb") -> Database:
    db = Database(tmp_path / name)
    db.begin()
    db.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, val TEXT)")
    db.commit()
    return db


def _make_replica(tmp_path: Path, name: str = "replica.hdb") -> Database:
    db = Database(tmp_path / name)
    return db


# ── Unit: PhysicalSubscriptionMeta ────────────────────────────────────────────

def test_physical_sub_meta_defaults():
    m = PhysicalSubscriptionMeta("s1", "http://localhost:9000")
    assert m.name == "s1"
    assert m.connection == "http://localhost:9000"
    assert m.last_lsn == 0
    assert m.auto_start is True


# ── Unit: pager phys_dirty tracking ───────────────────────────────────────────

def test_pager_tracks_phys_dirty(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db.begin()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'hello')")
    db.commit()

    assert db._pager._phys_dirty, "dirty pages should be tracked after commit"
    assert db._pager._phys_current_lsn > 0
    db.close()


def test_pager_no_phys_dirty_with_catalog_lsn_zero(tmp_path):
    """catalog_lsn=0 in commit() must not populate _phys_dirty."""
    db = Database(tmp_path / "p.hdb")
    db._pager.begin()
    db._pager.commit(min_consumed_lsn=0, catalog_lsn=0)
    assert db._pager._phys_dirty == {}
    db.close()


def test_pager_phys_checkpoint_lsn_advances(tmp_path):
    """After a checkpoint, _phys_checkpoint_lsn advances and _phys_dirty only
    contains pages modified *after* that checkpoint (not the stale pre-checkpoint ones)."""
    from hyperion.wal import WAL
    db = Database(tmp_path / "p.hdb")
    db.begin()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.commit()

    prev_checkpoint_lsn = db._pager._phys_checkpoint_lsn

    for i in range(WAL.CHECKPOINT_PAGES + 5):
        db.begin()
        db.execute(f"INSERT INTO t VALUES ({i}, 'x')")
        db.commit()

    # A checkpoint must have occurred
    assert db._pager._phys_checkpoint_lsn > prev_checkpoint_lsn
    # All pages currently in _phys_dirty must have lsn >= checkpoint lsn
    for pn, (lsn, _) in db._pager._phys_dirty.items():
        assert lsn >= db._pager._phys_checkpoint_lsn, (
            f"page {pn} has stale lsn {lsn} < checkpoint {db._pager._phys_checkpoint_lsn}"
        )
    db.close()


# ── Unit: database SQL commands ───────────────────────────────────────────────

def test_create_drop_physical_subscription(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db.execute("CREATE PHYSICAL SUBSCRIPTION s1 CONNECTION 'http://localhost:9001'")
    assert "s1" in db._phys_subs
    assert db._phys_subs["s1"].connection == "http://localhost:9001"

    # State file written
    state_path = tmp_path / "p.phys_state"
    assert state_path.exists()
    loaded = json.loads(state_path.read_text())
    assert len(loaded) == 1
    assert loaded[0]["name"] == "s1"

    db.execute("DROP PHYSICAL SUBSCRIPTION s1")
    assert "s1" not in db._phys_subs
    loaded2 = json.loads(state_path.read_text())
    assert loaded2 == []
    db.close()


def test_create_physical_subscription_if_not_exists(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db.execute("CREATE PHYSICAL SUBSCRIPTION s1 CONNECTION 'http://a:1'")
    db.execute("CREATE PHYSICAL SUBSCRIPTION IF NOT EXISTS s1 CONNECTION 'http://b:2'")
    assert db._phys_subs["s1"].connection == "http://a:1"
    db.close()


def test_drop_physical_subscription_if_exists(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db.execute("DROP PHYSICAL SUBSCRIPTION IF EXISTS nonexistent")
    db.close()


def test_show_master_status(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db.begin()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.commit()
    rows = list(db.execute("SHOW MASTER STATUS").fetchall())
    assert len(rows) == 1
    assert rows[0]["binlog_pos"] > 0
    # Before any checkpoint, data lives in the WAL file, not the db file
    assert rows[0]["wal_size"] > 0
    db.close()


def test_show_slave_status_empty(tmp_path):
    db = Database(tmp_path / "p.hdb")
    rows = list(db.execute("SHOW SLAVE STATUS").fetchall())
    assert rows == []
    db.close()


def test_show_binlog_empty_before_writes(tmp_path):
    db = Database(tmp_path / "p.hdb")
    rows = list(db.execute("SHOW BINLOG").fetchall())
    assert rows == []
    db.close()


def test_show_binlog_populated_after_write(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db.begin()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'hello')")
    db.commit()
    rows = list(db.execute("SHOW BINLOG").fetchall())
    assert len(rows) > 0
    assert all("page_num" in r and "catalog_lsn" in r for r in rows)
    db.close()


def test_stop_slave_promotes_to_writable(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db._readonly = True
    db.execute("CREATE PHYSICAL SUBSCRIPTION s1 CONNECTION 'http://localhost:1'")
    db._phys_workers.clear()  # no real worker to stop
    db._readonly = True
    db.stop_slave()
    assert not db._readonly
    db.close()


# ── Unit: phys_state persistence across open/close ───────────────────────────

def test_phys_subs_persist_across_reopen(tmp_path):
    db = Database(tmp_path / "p.hdb")
    db.execute("CREATE PHYSICAL SUBSCRIPTION s1 CONNECTION 'http://localhost:9001'")
    db.close()

    db2 = Database(tmp_path / "p.hdb")
    assert "s1" in db2._phys_subs
    assert db2._phys_subs["s1"].connection == "http://localhost:9001"
    db2.close()


# ── Integration: HTTP snapshot/changes endpoints ─────────────────────────────

def test_physical_snapshot_endpoint(tmp_path):
    primary = _make_primary(tmp_path)
    primary.begin()
    primary.execute("INSERT INTO t1 VALUES (1, 'row1')")
    primary.commit()

    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address
    try:
        snapshot = _fetch_snapshot(f"http://{host}:{port}")
        assert "lsn" in snapshot
        assert "db_data" in snapshot
        assert isinstance(snapshot["lsn"], int)
        db_bytes  = base64.b64decode(snapshot["db_data"])
        wal_bytes = base64.b64decode(snapshot["wal_data"]) if snapshot.get("wal_data") else b""
        # Before checkpoint, data lives in WAL rather than db file — at least one must be non-empty
        assert len(db_bytes) > 0 or len(wal_bytes) > 0
    finally:
        srv.shutdown()
        primary.close()


def test_physical_changes_endpoint_no_changes(tmp_path):
    primary = _make_primary(tmp_path)
    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address
    try:
        result = _fetch_changes(f"http://{host}:{port}", since_lsn=primary._catalog.lsn)
        assert "lsn" in result
        pages = result.get("pages") or []
        assert isinstance(pages, list)
    finally:
        srv.shutdown()
        primary.close()


def test_physical_changes_snapshot_required_for_lsn_zero(tmp_path):
    primary = _make_primary(tmp_path)
    primary.begin()
    primary.execute("INSERT INTO t1 VALUES (1, 'a')")
    primary.commit()

    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address
    try:
        result = _fetch_changes(f"http://{host}:{port}", since_lsn=0)
        assert result.get("snapshot_required") is True
    finally:
        srv.shutdown()
        primary.close()


def test_physical_changes_incremental(tmp_path):
    primary = _make_primary(tmp_path)
    base_lsn = primary._catalog.lsn

    primary.begin()
    primary.execute("INSERT INTO t1 VALUES (1, 'first')")
    primary.commit()

    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address
    try:
        result = _fetch_changes(f"http://{host}:{port}", since_lsn=base_lsn)
        if not result.get("snapshot_required"):
            pages = result.get("pages") or []
            for p in pages:
                assert "page_num" in p
                assert "data" in p
                base64.b64decode(p["data"])  # must be valid base64
    finally:
        srv.shutdown()
        primary.close()


# ── Integration: apply_snapshot ───────────────────────────────────────────────

def test_apply_snapshot_replicates_data(tmp_path):
    primary = _make_primary(tmp_path, "primary.hdb")
    primary.begin()
    primary.execute("INSERT INTO t1 VALUES (1, 'hello')")
    primary.execute("INSERT INTO t1 VALUES (2, 'world')")
    primary.commit()

    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address
    try:
        snapshot = _fetch_snapshot(f"http://{host}:{port}")
    finally:
        srv.shutdown()
        primary.close()

    replica = _make_replica(tmp_path, "replica.hdb")
    replica._phys_subs["s1"] = PhysicalSubscriptionMeta("s1", "http://stub")
    apply_snapshot(replica, snapshot, "s1")

    rows = list(replica.execute("SELECT * FROM t1 ORDER BY id").fetchall())
    assert len(rows) == 2
    assert rows[0]["val"] == "hello"
    assert rows[1]["val"] == "world"
    replica.close()


def test_apply_incremental_no_pages_updates_lsn(tmp_path):
    primary = _make_primary(tmp_path)
    replica = _make_replica(tmp_path, "replica.hdb")
    replica._phys_subs["s1"] = PhysicalSubscriptionMeta("s1", "http://stub")
    replica._phys_subs["s1"].last_lsn = 5

    apply_incremental(replica, {"lsn": 10, "pages": []}, "s1")
    assert replica._phys_subs["s1"].last_lsn == 10
    replica.close()
    primary.close()


# ── Integration: end-to-end auto-sync via PhysicalReplicationWorker ───────────

def test_worker_syncs_primary_to_replica(tmp_path):
    primary = _make_primary(tmp_path, "primary.hdb")
    primary.begin()
    for i in range(5):
        primary.execute(f"INSERT INTO t1 VALUES ({i}, 'r{i}')")
    primary.commit()

    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address

    replica = _make_replica(tmp_path, "replica.hdb")
    replica._phys_subs["s1"] = PhysicalSubscriptionMeta(
        "s1", f"http://{host}:{port}"
    )

    worker = PhysicalReplicationWorker(replica, "s1", poll_interval=0.1)
    worker.start()

    # Wait up to 3 s for sync
    deadline = time.time() + 3
    while time.time() < deadline:
        if replica._phys_subs["s1"].last_lsn > 0:
            break
        time.sleep(0.05)

    worker.stop()
    srv.shutdown()

    assert replica._phys_subs["s1"].last_lsn > 0, "replica never synced"
    rows = list(replica.execute("SELECT * FROM t1 ORDER BY id").fetchall())
    assert len(rows) == 5
    assert not replica._readonly, "STOP worker should re-enable writes"

    replica.close()
    primary.close()


def test_worker_readonly_while_running(tmp_path):
    primary = _make_primary(tmp_path, "primary.hdb")
    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address

    replica = _make_replica(tmp_path, "replica.hdb")
    replica._phys_subs["s1"] = PhysicalSubscriptionMeta(
        "s1", f"http://{host}:{port}"
    )

    worker = PhysicalReplicationWorker(replica, "s1", poll_interval=60)
    worker.start()
    assert replica._readonly is True
    worker.stop()
    assert replica._readonly is False

    srv.shutdown()
    replica.close()
    primary.close()


def test_worker_auto_reconnect_on_error(tmp_path):
    """Worker should move to 'Retrying' on connection failure, not crash."""
    replica = _make_replica(tmp_path, "replica.hdb")
    replica._phys_subs["bad"] = PhysicalSubscriptionMeta(
        "bad", "http://127.0.0.1:1"  # nothing listening here
    )
    worker = PhysicalReplicationWorker(replica, "bad", poll_interval=0.05)
    worker.start()
    time.sleep(0.2)
    assert worker.status in ("Retrying", "Running")
    assert worker._thread.is_alive()
    worker.stop()
    replica.close()


# ── Integration: promotion (STOP SLAVE makes replica writable) ────────────────

def test_start_stop_slave_sql(tmp_path):
    primary = _make_primary(tmp_path, "primary.hdb")
    srv = HTTPServerMode(primary, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address

    replica = _make_replica(tmp_path, "replica.hdb")
    replica.execute(
        f"CREATE PHYSICAL SUBSCRIPTION s1 CONNECTION 'http://{host}:{port}'"
    )

    replica.execute("START SLAVE s1")
    assert replica._readonly is True
    time.sleep(0.3)

    replica.execute("STOP SLAVE s1")
    assert replica._readonly is False

    srv.shutdown()
    replica.close()
    primary.close()
