"""Overflow page chain crash-recovery tests (Priority 4 audit item).

Scenario: 9 large rows (each requiring 3 overflow pages) are committed
cleanly.  The 10th row's commit is interrupted mid-frame — simulating an
OS crash or power loss after some WAL frames have been written but before
the COMMIT_PN marker is appended.

On the next open, replay_if_exists must:
  (a) discard the partial 10th transaction (no COMMIT_PN → uncommitted),
  (b) preserve the 9 already-committed rows intact, and
  (c) leave the database in a usable state with no dangling overflow refs.
"""

import struct
from pathlib import Path

import pytest
from hyperion import Database
from hyperion.constants import OVERFLOW_DATA_SZ, ROW_INLINE_CAP
from hyperion.wal import WAL


# Large enough to require exactly 3 overflow pages.
# ROW_INLINE_CAP = 191; each overflow page holds OVERFLOW_DATA_SZ = 4083 bytes.
_PAYLOAD = "y" * (ROW_INLINE_CAP + 1 + 2 * OVERFLOW_DATA_SZ + 100)


def _open_db(path: str) -> Database:
    return Database(path)


def _force_crash_close(db) -> None:
    """Simulate an OS crash: close raw file handles and release the pager's
    intra-process write lock without going through commit/rollback.

    The WAL file is intentionally left dirty on disk so that the next open
    can exercise the crash-recovery path.
    """
    if db is None:
        return
    try:
        pager = db._pager
        if pager._wal is not None:
            pager._wal._file.flush()
            pager._wal._file.close()
            pager._wal = None
        pager._file.flush()
        pager._file.close()
        # Release the write lock that pager.begin() acquired.
        # Normally released by commit/rollback; must be done explicitly
        # when simulating a crash that skips those paths.
        wl = getattr(pager, '_write_lock', None)
        if wl is not None and wl.locked():
            wl.release()
        pager._in_txn = False
    except Exception:
        pass


class TestOverflowCrashRecovery:

    def test_partial_commit_discarded_on_reopen(self, tmp_path):
        """WAL truncated mid-frame during a large-row commit: the 10th row
        must not appear after recovery, but the 9 prior rows must survive."""
        db_path = str(tmp_path / "test.hdb")
        wal_path = Path(db_path).with_suffix(".wal")

        # --- Phase 1: insert 9 rows cleanly ------------------------------------
        db = _open_db(db_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data TEXT)")
        for i in range(9):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, _PAYLOAD))
        db.close()
        assert not wal_path.exists(), "WAL must be gone after a clean close"

        # --- Phase 2: simulate a crash mid-commit of the 10th row --------------
        # Monkeypatch commit_txn to write 3 complete frames then half a 4th
        # frame and raise without ever writing the COMMIT_PN marker.
        original_commit = WAL.commit_txn
        wal_file_ref: list = [None]

        def _crashing_commit(self_wal, working: dict):
            frames_written = 0
            for pn, data in working.items():
                frame = struct.pack("<I", pn) + bytes(data)
                if frames_written < 3:
                    self_wal._file.write(frame)            # full frame
                elif frames_written == 3:
                    # mid-frame truncation: write only the first half
                    self_wal._file.write(frame[: WAL.FRAME_SZ // 2])
                    frames_written += 1
                    break
                frames_written += 1
            self_wal._file.flush()
            wal_file_ref[0] = self_wal
            raise OSError("simulated crash mid-commit")

        WAL.commit_txn = _crashing_commit
        db2 = None
        try:
            db2 = _open_db(db_path)
            with pytest.raises(OSError, match="simulated crash"):
                db2.execute("INSERT INTO t VALUES (?, ?)", (9, _PAYLOAD))
        finally:
            WAL.commit_txn = original_commit
            _force_crash_close(db2)

        assert wal_path.exists(), "WAL file must survive the simulated crash"

        # --- Phase 3: reopen — crash recovery path -----------------------------
        db3 = _open_db(db_path)

        # (a) partially-written row must not be visible
        count = db3.execute("SELECT COUNT(*) AS c FROM t WHERE id = 9").fetchone()["c"]
        assert count == 0, "row 9 must be invisible after recovery (uncommitted)"

        # (b) all 9 prior rows must be intact
        rows = db3.execute("SELECT id FROM t ORDER BY id").fetchall()
        assert len(rows) == 9, f"expected 9 rows, got {len(rows)}"
        assert [r["id"] for r in rows] == list(range(9))

        # Spot-check that a large-value row round-trips correctly (overflow chain)
        row = db3.execute("SELECT data FROM t WHERE id = 4").fetchone()
        assert row["data"] == _PAYLOAD, "large overflow value must survive recovery"

        # (c) no dangling overflow refs — db must accept new large-row inserts
        db3.execute("INSERT INTO t VALUES (?, ?)", (99, _PAYLOAD))
        new_count = db3.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"]
        assert new_count == 10

        db3.close()

    def test_complete_wal_no_commit_marker_discarded(self, tmp_path):
        """WAL has N complete frames but no COMMIT_PN: all frames discarded,
        9 prior rows survive."""
        db_path = str(tmp_path / "test.hdb")
        wal_path = Path(db_path).with_suffix(".wal")

        db = _open_db(db_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data TEXT)")
        for i in range(9):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, _PAYLOAD))
        db.close()

        original_commit = WAL.commit_txn

        def _commit_no_marker(self_wal, working: dict):
            # Write all frames but omit the COMMIT_PN
            for pn, data in working.items():
                self_wal._file.write(struct.pack("<I", pn) + bytes(data))
            self_wal._file.flush()
            raise OSError("simulated crash before commit marker")

        WAL.commit_txn = _commit_no_marker
        db2 = None
        try:
            db2 = _open_db(db_path)
            with pytest.raises(OSError):
                db2.execute("INSERT INTO t VALUES (?, ?)", (9, _PAYLOAD))
        finally:
            WAL.commit_txn = original_commit
            _force_crash_close(db2)

        db3 = _open_db(db_path)
        rows = db3.execute("SELECT id FROM t ORDER BY id").fetchall()
        assert len(rows) == 9
        assert [r["id"] for r in rows] == list(range(9))
        db3.close()
