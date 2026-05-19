import os
import struct
from pathlib import Path

from .constants import PAGE_SIZE


class WAL:
    MAGIC    = b"HWAL"
    HDR_SIZE = 8
    FRAME_SZ = 4 + PAGE_SIZE

    def __init__(self, path: Path):
        self._path = path
        self._file = open(path, "w+b")
        self._file.write(self.MAGIC + b"\x00\x00\x00\x00")
        self._file.flush()

    @classmethod
    def replay_if_exists(cls, wal_path: Path, db_file) -> None:
        if not wal_path.exists():
            return
        try:
            with open(wal_path, "rb") as wf:
                hdr = wf.read(cls.HDR_SIZE)
                if len(hdr) < cls.HDR_SIZE or hdr[:4] != cls.MAGIC or hdr[4] != 1:
                    return  # corrupt or uncommitted — discard
                while True:
                    frame = wf.read(cls.FRAME_SZ)
                    if len(frame) < cls.FRAME_SZ:
                        break
                    pn = struct.unpack_from("I", frame)[0]
                    db_file.seek(pn * PAGE_SIZE)
                    db_file.write(frame[4:])
                db_file.flush()
        finally:
            wal_path.unlink(missing_ok=True)

    def commit(self, dirty: dict[int, bytearray], db_file) -> None:
        for pn, data in dirty.items():
            self._file.write(struct.pack("I", pn) + bytes(data))
        self._file.seek(4)
        self._file.write(b"\x01")   # committed
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError:
            pass
        for pn, data in dirty.items():
            db_file.seek(pn * PAGE_SIZE)
            db_file.write(data)
        db_file.flush()
        self._file.close()
        self._path.unlink(missing_ok=True)

    def rollback(self) -> None:
        self._file.close()
        self._path.unlink(missing_ok=True)
