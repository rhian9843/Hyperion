"""Per-page CRC-32 checksums.

Every page reserves its last PAGE_CKSUM_SZ bytes for a CRC-32 of the
preceding content (bytes 0 .. PAGE_CKSUM_OFF).  A stored value of zero
means the page pre-dates checksums and is accepted without verification.
"""
import struct
from binascii import crc32 as _crc32

from .constants import PAGE_CKSUM_OFF


class CorruptPageError(RuntimeError):
    """Raised when a page's stored CRC does not match its computed CRC."""
    def __init__(self, page_num: int, stored: int, computed: int) -> None:
        super().__init__(
            f"page {page_num}: checksum mismatch "
            f"(stored 0x{stored:08x}, computed 0x{computed:08x})")
        self.page_num = page_num
        self.stored   = stored
        self.computed = computed


def page_checksum(data: bytes | bytearray) -> int:
    """CRC-32 of page content (bytes 0 .. PAGE_CKSUM_OFF)."""
    return _crc32(data[:PAGE_CKSUM_OFF]) & 0xFFFF_FFFF


def stamp_page(page: bytearray) -> None:
    """Write CRC-32 into the checksum slot (last 4 bytes of the page)."""
    struct.pack_into("<I", page, PAGE_CKSUM_OFF, page_checksum(page))


def verify_page(data: bytes | bytearray, page_num: int) -> None:
    """Raise CorruptPageError if the stored checksum does not match.

    A stored checksum of zero is treated as 'no checksum' (legacy page)
    and passes without verification.
    """
    stored = struct.unpack_from("<I", data, PAGE_CKSUM_OFF)[0]
    if stored == 0:
        return  # legacy page — no checksum was written
    computed = page_checksum(data)
    if stored != computed:
        raise CorruptPageError(page_num, stored, computed)
