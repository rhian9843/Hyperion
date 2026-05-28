import os
import struct
from pathlib import Path

from .constants import PAGE_SIZE


class WAL:
    """Persistent write-ahead log shared across multiple transactions.

    Format
    ------
    Header : MAGIC (4 bytes) + version (4 bytes, little-endian uint32)
    Frames : repeated blocks of (page_num: uint32)(page_data: PAGE_SIZE bytes)
    Commit  : a frame with page_num == COMMIT_PN (0xFFFF_FFFF) marks the end
              of one committed transaction; its page_data bytes are ignored.

    Versions
    --------
    0x01  Legacy: single committed transaction, all frames applied directly.
    0x02  Current: multi-transaction; use COMMIT_PN markers.

    A transaction whose frames are not followed by a COMMIT_PN marker is
    considered uncommitted and is discarded on crash recovery.
    """

    MAGIC            = b"HWAL"
    HDR_SIZE         = 8
    FRAME_SZ         = 4 + PAGE_SIZE
    COMMIT_PN        = 0xFFFF_FFFF
    CHECKPOINT_PAGES = 64  # force checkpoint after this many accumulated pages

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
        self._file.seek(0, 2)  # position at end for appending
        self._pages_since_ckpt: int = 0

    @staticmethod
    def _create(path: Path):
        f = open(path, "w+b")
        f.write(WAL.MAGIC + struct.pack("<I", 2))  # version 2
        f.flush()
        return f

    # ── Transaction boundary ───────────────────────────────────────────────────

    def begin_offset(self) -> int:
        """Return current WAL size (used to roll back an uncommitted transaction)."""
        return self._file.seek(0, 2)

    def commit_txn(self, working: dict[int, bytearray]) -> None:
        """Append all dirty pages followed by a commit marker, then fsync."""
        for pn, data in working.items():
            self._file.write(struct.pack("<I", pn) + bytes(data))
        self._file.write(struct.pack("<I", self.COMMIT_PN) + bytes(PAGE_SIZE))
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError:
            pass
        self._pages_since_ckpt += len(working)

    def rollback_txn(self, offset: int) -> None:
        """Truncate the WAL back to *offset* (discards the current transaction)."""
        self._file.truncate(offset)
        self._file.seek(0, 2)

    # ── Checkpoint ────────────────────────────────────────────────────────────

    def needs_checkpoint(self) -> bool:
        return self._pages_since_ckpt >= self.CHECKPOINT_PAGES

    def checkpoint(self, db_file) -> None:
        """Apply all committed WAL transactions to *db_file*, then truncate WAL."""
        self._file.seek(self.HDR_SIZE)
        pending: list[tuple[int, bytes]] = []
        while True:
            frame = self._file.read(self.FRAME_SZ)
            if len(frame) < self.FRAME_SZ:
                break
            pn = struct.unpack_from("<I", frame)[0]
            if pn == self.COMMIT_PN:
                for ppn, data in pending:
                    db_file.seek(ppn * PAGE_SIZE)
                    db_file.write(data)
                pending.clear()
            else:
                pending.append((pn, frame[4:]))
        # Trailing uncommitted frames (no COMMIT_PN) are discarded.
        db_file.flush()
        try:
            os.fsync(db_file.fileno())
        except OSError:
            pass
        # Truncate WAL to just the header; keep file open for next transactions.
        self._file.seek(self.HDR_SIZE)
        self._file.truncate()
        self._file.flush()
        self._pages_since_ckpt = 0

    def close(self) -> None:
        self._file.flush()
        self._file.close()

    # ── Crash recovery (called at Pager startup) ───────────────────────────────

    @classmethod
    def replay_if_exists(cls, wal_path: Path, db_file) -> None:
        """Replay any committed transactions from a WAL left by a previous run."""
        if not wal_path.exists():
            return
        try:
            with open(wal_path, "rb") as wf:
                hdr = wf.read(cls.HDR_SIZE)
                if len(hdr) < cls.HDR_SIZE or hdr[:4] != cls.MAGIC:
                    return
                version = struct.unpack_from("<I", hdr, 4)[0]
                if version == 1:
                    # Legacy: single committed transaction — apply all frames.
                    while True:
                        frame = wf.read(cls.FRAME_SZ)
                        if len(frame) < cls.FRAME_SZ:
                            break
                        pn = struct.unpack_from("<I", frame)[0]
                        db_file.seek(pn * PAGE_SIZE)
                        db_file.write(frame[4:])
                    db_file.flush()
                elif version == 2:
                    # Multi-txn: apply only transactions terminated by COMMIT_PN.
                    pending: list[tuple[int, bytes]] = []
                    while True:
                        frame = wf.read(cls.FRAME_SZ)
                        if len(frame) < cls.FRAME_SZ:
                            break
                        pn = struct.unpack_from("<I", frame)[0]
                        if pn == cls.COMMIT_PN:
                            for ppn, data in pending:
                                db_file.seek(ppn * PAGE_SIZE)
                                db_file.write(data)
                            pending.clear()
                        else:
                            pending.append((pn, frame[4:]))
                    db_file.flush()
                # Any other version: discard (treat as corrupt).
        finally:
            wal_path.unlink(missing_ok=True)
