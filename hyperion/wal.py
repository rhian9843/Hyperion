import json
import os
import struct
import threading
from pathlib import Path

from .constants import PAGE_SIZE
from .checksum import verify_page


class WAL:
    """Persistent write-ahead log shared across multiple transactions.

    Frame format (all versions)
    ---------------------------
    Header : MAGIC (4 bytes) + version (4 bytes, little-endian uint32)
    Frame  : page_num (4 bytes) + frame_data (PAGE_SIZE bytes)
    Commit : page_num == COMMIT_PN (0xFFFF_FFFF) — marks end of transaction

    Versions
    --------
    0x01  Legacy: single committed transaction, all frames applied directly.
    0x02  Multi-transaction; COMMIT_PN markers separate transactions.
    0x03  Adds LOGICAL_PN (0xFFFF_FFFE) frames for logical replication.
          A LOGICAL frame carries a variable-length JSON changelog entry:
            payload[0:2]  = data_len (uint16 LE)
            payload[2:2+data_len] = UTF-8 JSON of the changelog entry dict
            payload[2+data_len:]  = zero padding

    Logical replication — single-log design
    ----------------------------------------
    DML    →  stage_logical(entry_dict)          [in memory, zero I/O]
    COMMIT →  commit_txn(working_pages)
                LOGICAL frames + PAGE frames + COMMIT_PN in one fsync.
                The row change and its changelog record are atomic.
    CHECKPOINT(min_consumed_lsn) →
                Apply PAGE frames to db_file (crash recovery payload).
                Retain LOGICAL frames whose lsn > min_consumed_lsn in the WAL.
                Rewrite WAL: HDR + [retained LOGICAL frames] + COMMIT_PN.
                No separate archive file needed — the WAL IS the replication log.
    CRASH RECOVERY →
                replay_if_exists applies PAGE frames, returns LOGICAL entries
                so the caller can re-stage them into the fresh WAL.

    The min_consumed_lsn passed to checkpoint is:
        min(sub.last_lsn for sub in active_subscriptions)   if any subscriptions
        catalog.lsn                                          if no subscriptions
    The second case causes all LOGICAL frames to be discarded (lsn <= catalog.lsn
    for every entry), so the WAL shrinks back to just its header.
    """

    MAGIC            = b"HWAL"
    HDR_SIZE         = 8
    FRAME_SZ         = 4 + PAGE_SIZE
    COMMIT_PN        = 0xFFFF_FFFF
    LOGICAL_PN       = 0xFFFF_FFFE
    CHECKPOINT_PAGES = 64
    _LOGICAL_DATA_SZ = PAGE_SIZE - 2   # max JSON bytes per logical frame

    def __init__(self, path: Path) -> None:
        self._path = path
        if path.exists():
            f = open(path, "r+b")
            hdr = f.read(self.HDR_SIZE)
            if len(hdr) >= 4 and hdr[:4] == self.MAGIC:
                self._file = f
            else:
                f.close()
                self._file = self._create(path)
        else:
            self._file = self._create(path)
        self._file.seek(0, 2)
        self._pages_since_ckpt: int = 0
        self._pending_logical:  list = []  # staged for current uncommitted txn
        # All retained logical entries (in-memory mirror of WAL logical content).
        # Written by commit_txn; filtered/trimmed by checkpoint.
        # Protected by _committed_lock for concurrent HTTP reads.
        self._committed_logical: list = []
        self._committed_lock = threading.Lock()

    @staticmethod
    def _create(path: Path):
        f = open(path, "w+b")
        f.write(WAL.MAGIC + struct.pack("<I", 3))  # version 3: LOGICAL_PN support
        f.flush()
        return f

    # ── Transaction boundary ───────────────────────────────────────────────────

    def begin_offset(self) -> int:
        return self._file.seek(0, 2)

    def stage_logical(self, entry: dict) -> None:
        """Stage a logical record for the next commit — no I/O until commit_txn."""
        json_bytes = json.dumps(entry, default=str).encode()
        if len(json_bytes) <= self._LOGICAL_DATA_SZ:
            self._pending_logical.append(entry)
        # Rows whose JSON exceeds one frame are silently skipped.

    def commit_txn(self, working: dict[int, bytearray]) -> None:
        """Write logical + page frames + commit marker in one fsync.

        The logical and page writes land in the same fsync — atomicity is free.
        A crash before the fsync rolls back both; a crash after retains both.
        """
        new_committed: list = []
        for entry in self._pending_logical:
            json_bytes = json.dumps(entry, default=str).encode()
            payload = bytearray(PAGE_SIZE)
            struct.pack_into("<H", payload, 0, len(json_bytes))
            payload[2:2 + len(json_bytes)] = json_bytes
            self._file.write(struct.pack("<I", self.LOGICAL_PN) + bytes(payload))
            new_committed.append(entry)
        self._pending_logical.clear()

        for pn, data in working.items():
            self._file.write(struct.pack("<I", pn) + bytes(data))
        self._file.write(struct.pack("<I", self.COMMIT_PN) + bytes(PAGE_SIZE))
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError:
            pass

        if new_committed:
            with self._committed_lock:
                self._committed_logical.extend(new_committed)
        self._pages_since_ckpt += len(working)

    def rollback_txn(self, offset: int) -> None:
        self._pending_logical.clear()
        self._file.truncate(offset)
        self._file.seek(0, 2)

    # ── Checkpoint ────────────────────────────────────────────────────────────

    def needs_checkpoint(self) -> bool:
        return self._pages_since_ckpt >= self.CHECKPOINT_PAGES

    def checkpoint(self, db_file, min_consumed_lsn: int = 0) -> None:
        """Apply committed PAGE frames to db_file; retain unconsumed LOGICAL frames.

        PAGE frames (crash-recovery payload) are applied and removed.
        LOGICAL frames with lsn > min_consumed_lsn are kept by rewriting the WAL
        as: HDR + [retained logical frames] + COMMIT_PN.  No archive file needed.

        Crash safety
        ------------
        1. Scan WAL, collect page frames and retained logical entries.
        2. Apply page frames to db_file + fsync  (pages now durable).
        3. Rewrite WAL with retained logical frames + truncate + flush.

        If the process crashes between steps 2 and 3, the WAL still has the old
        content (page frames already applied).  On next open, replay_if_exists
        re-applies the page frames (idempotent) and re-extracts the logical frames.
        If it crashes during step 3, the WAL has partially overwritten content;
        the page data is still durable from step 2, and replay re-applies safely.
        """
        self._file.seek(self.HDR_SIZE)
        pending_pages:   list = []
        pending_logical: list = []
        retained:        list = []   # lsn > min_consumed_lsn

        while True:
            frame = self._file.read(self.FRAME_SZ)
            if len(frame) < self.FRAME_SZ:
                break
            pn = struct.unpack_from("<I", frame)[0]
            if pn == self.COMMIT_PN:
                for ppn, data in pending_pages:
                    verify_page(data, ppn)
                    db_file.seek(ppn * PAGE_SIZE)
                    db_file.write(data)
                for entry in pending_logical:
                    if entry.get("lsn", 0) > min_consumed_lsn:
                        retained.append(entry)
                pending_pages.clear()
                pending_logical.clear()
            elif pn == self.LOGICAL_PN:
                data_len = struct.unpack_from("<H", frame, 4)[0]
                raw = frame[6:6 + data_len]
                try:
                    pending_logical.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
            else:
                pending_pages.append((pn, frame[4:]))
        # Trailing uncommitted frames (no COMMIT_PN) are discarded.

        db_file.flush()
        try:
            os.fsync(db_file.fileno())
        except OSError:
            pass

        # Rewrite WAL: HDR stays; body = retained logical frames + COMMIT_PN.
        self._file.seek(self.HDR_SIZE)
        if retained:
            for entry in retained:
                json_bytes = json.dumps(entry, default=str).encode()
                payload = bytearray(PAGE_SIZE)
                struct.pack_into("<H", payload, 0, len(json_bytes))
                payload[2:2 + len(json_bytes)] = json_bytes
                self._file.write(struct.pack("<I", self.LOGICAL_PN) + bytes(payload))
            # Single COMMIT_PN covers all retained frames as one committed block.
            self._file.write(struct.pack("<I", self.COMMIT_PN) + bytes(PAGE_SIZE))
        self._file.truncate()
        self._file.flush()
        self._pages_since_ckpt = 0

        # Keep in-memory mirror consistent with the rewritten WAL.
        with self._committed_lock:
            self._committed_logical = retained

        # Leave write head at end for subsequent commits.
        self._file.seek(0, 2)

    def close(self) -> None:
        self._file.flush()
        self._file.close()

    # ── Crash recovery ────────────────────────────────────────────────────────

    @classmethod
    def replay_if_exists(cls, wal_path: Path, db_file) -> list[dict]:
        """Replay committed PAGE frames from a crash-surviving WAL.

        Returns LOGICAL entries found in committed transactions so the caller
        can re-stage them into the fresh WAL (preserving retention across restarts).

        Durability ordering
        -------------------
        1. Apply all committed PAGE frames to db_file.
        2. flush() + fsync(db_file) — pages durable before WAL is touched.
        3. Unlink WAL — safe because every committed page byte is now in db_file.
           Retained logical entries are returned to the caller for re-staging.
        """
        if not wal_path.exists():
            return []
        logical_out: list[dict] = []
        replayed = False
        with open(wal_path, "rb") as wf:
            hdr = wf.read(cls.HDR_SIZE)
            if len(hdr) < cls.HDR_SIZE or hdr[:4] != cls.MAGIC:
                wal_path.unlink(missing_ok=True)
                return []
            version = struct.unpack_from("<I", hdr, 4)[0]
            if version == 1:
                while True:
                    frame = wf.read(cls.FRAME_SZ)
                    if len(frame) < cls.FRAME_SZ:
                        break
                    pn = struct.unpack_from("<I", frame)[0]
                    if pn not in (cls.COMMIT_PN, cls.LOGICAL_PN):
                        db_file.seek(pn * PAGE_SIZE)
                        db_file.write(frame[4:])
                replayed = True
            elif version in (2, 3):
                pending_pages:   list = []
                pending_logical: list = []
                while True:
                    frame = wf.read(cls.FRAME_SZ)
                    if len(frame) < cls.FRAME_SZ:
                        break
                    pn = struct.unpack_from("<I", frame)[0]
                    if pn == cls.COMMIT_PN:
                        for ppn, data in pending_pages:
                            verify_page(data, ppn)
                            db_file.seek(ppn * PAGE_SIZE)
                            db_file.write(data)
                        logical_out.extend(pending_logical)
                        pending_pages.clear()
                        pending_logical.clear()
                        replayed = True
                    elif pn == cls.LOGICAL_PN and version == 3:
                        data_len = struct.unpack_from("<H", frame, 4)[0]
                        raw = frame[6:6 + data_len]
                        try:
                            pending_logical.append(json.loads(raw))
                        except json.JSONDecodeError:
                            pass
                    else:
                        pending_pages.append((pn, frame[4:]))

        if replayed:
            db_file.flush()
            try:
                os.fsync(db_file.fileno())
            except OSError:
                pass

        wal_path.unlink(missing_ok=True)
        return logical_out
