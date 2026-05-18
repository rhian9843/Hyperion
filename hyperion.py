#!/usr/bin/env python3
"""Hyperion — a minimal SQLite clone in Python."""

import struct
import sys
from dataclasses import dataclass
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

COLUMN_USERNAME_SIZE = 32
COLUMN_EMAIL_SIZE    = 255
TABLE_MAX_PAGES      = 100
PAGE_SIZE            = 4096  # matches OS virtual-memory page size

# Binary row layout: uint32 id | 33-byte username | 256-byte email
ROW_FORMAT   = "I33s256s"
ROW_SIZE     = struct.calcsize(ROW_FORMAT)   # 293 bytes
ROWS_PER_PAGE = PAGE_SIZE // ROW_SIZE        # 13
TABLE_MAX_ROWS = ROWS_PER_PAGE * TABLE_MAX_PAGES


# ── Row ───────────────────────────────────────────────────────────────────────

@dataclass
class Row:
    id: int
    username: str
    email: str

    def __str__(self) -> str:
        return f"({self.id}, {self.username}, {self.email})"


def serialize_row(row: Row) -> bytes:
    return struct.pack(ROW_FORMAT, row.id, row.username.encode(), row.email.encode())


def deserialize_row(data: bytes) -> Row:
    id_, username, email = struct.unpack(ROW_FORMAT, data)
    return Row(
        id=id_,
        username=username.rstrip(b"\x00").decode(),
        email=email.rstrip(b"\x00").decode(),
    )


# ── Pager ─────────────────────────────────────────────────────────────────────

class Pager:
    def __init__(self, path: Path):
        self._file = open(path, "r+b" if path.exists() else "w+b")
        self._file.seek(0, 2)
        self.file_length = self._file.tell()
        self._pages: list[bytearray | None] = [None] * TABLE_MAX_PAGES

    def get_page(self, page_num: int) -> bytearray:
        if page_num >= TABLE_MAX_PAGES:
            raise RuntimeError(f"Page {page_num} out of bounds (max {TABLE_MAX_PAGES})")

        if self._pages[page_num] is None:
            page = bytearray(PAGE_SIZE)
            num_pages = self.file_length // PAGE_SIZE + bool(self.file_length % PAGE_SIZE)

            if page_num < num_pages:
                self._file.seek(page_num * PAGE_SIZE)
                data = self._file.read(PAGE_SIZE)
                page[: len(data)] = data

            self._pages[page_num] = page

        return self._pages[page_num]

    def _flush(self, page_num: int, size: int):
        page = self._pages[page_num]
        if page is None:
            raise RuntimeError("Tried to flush a null page")
        self._file.seek(page_num * PAGE_SIZE)
        self._file.write(page[:size])

    def close(self, row_count: int):
        full_pages, leftover = divmod(row_count, ROWS_PER_PAGE)

        for i in range(full_pages):
            if self._pages[i] is not None:
                self._flush(i, PAGE_SIZE)

        if leftover and self._pages[full_pages] is not None:
            self._flush(full_pages, leftover * ROW_SIZE)

        self._file.close()


# ── Table ─────────────────────────────────────────────────────────────────────

class Table:
    def __init__(self, path: Path):
        self._pager = Pager(path)
        self.row_count = self._pager.file_length // ROW_SIZE

    def _row_slot(self, row_num: int) -> tuple[bytearray, int]:
        page = self._pager.get_page(row_num // ROWS_PER_PAGE)
        byte_offset = (row_num % ROWS_PER_PAGE) * ROW_SIZE
        return page, byte_offset

    def insert(self, row: Row):
        if self.row_count >= TABLE_MAX_ROWS:
            print("Error: the table is full.")
            return
        page, offset = self._row_slot(self.row_count)
        page[offset : offset + ROW_SIZE] = serialize_row(row)
        self.row_count += 1
        print("Executed.")

    def select_all(self):
        for i in range(self.row_count):
            page, offset = self._row_slot(i)
            print(deserialize_row(bytes(page[offset : offset + ROW_SIZE])))
        print("Executed.")

    def close(self):
        self._pager.close(self.row_count)


# ── Parser ────────────────────────────────────────────────────────────────────

class ParseError(ValueError):
    pass


def parse_insert(text: str) -> Row:
    parts = text.split()
    if len(parts) != 4:
        raise ParseError("usage: insert <id> <username> <email>")
    _, id_str, username, email = parts
    try:
        id_ = int(id_str)
    except ValueError:
        raise ParseError(f"invalid id: {id_str!r}")
    if id_ < 0:
        raise ParseError("id cannot be negative")
    if len(username) > COLUMN_USERNAME_SIZE:
        raise ParseError(f"username too long (max {COLUMN_USERNAME_SIZE})")
    if len(email) > COLUMN_EMAIL_SIZE:
        raise ParseError(f"email too long (max {COLUMN_EMAIL_SIZE})")
    return Row(id=id_, username=username, email=email)


def handle(text: str, table: Table):
    lower = text.lower()
    if lower.startswith("insert"):
        table.insert(parse_insert(text))
    elif lower.startswith("select"):
        table.select_all()
    else:
        raise ParseError(f"unrecognized keyword at the start of '{text}'")


# ── REPL ──────────────────────────────────────────────────────────────────────

def repl(table: Table):
    while True:
        try:
            text = input("H > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue

        if text.startswith("."):
            if text == ".exit":
                break
            print(f"Unrecognized command: {text!r}")
            continue

        try:
            handle(text, table)
        except ParseError as e:
            print(f"Parse error: {e}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python hyperion.py <database_file>")
        sys.exit(1)

    table = Table(Path(sys.argv[1]))
    try:
        repl(table)
    finally:
        table.close()


if __name__ == "__main__":
    main()
