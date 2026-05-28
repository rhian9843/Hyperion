"""AsyncDatabase and AsyncCursor: asyncio-friendly wrappers around Database/Cursor.

All blocking database operations are offloaded to the event loop's default
thread-pool executor via run_in_executor so they never stall the event loop.
"""
from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database
    from .cursor import Cursor


class AsyncCursor:
    """Async wrapper around a synchronous Cursor."""

    def __init__(self, cursor: "Cursor") -> None:
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    async def fetchone(self) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._cursor.fetchone)

    async def fetchmany(self, size: int = 1) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._cursor.fetchmany(size))

    async def fetchall(self) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._cursor.fetchall)

    def close(self) -> None:
        self._cursor.close()

    def __aiter__(self) -> "AsyncCursor":
        return self

    async def __anext__(self) -> Any:
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row


class _AsyncReadOnlyContext:
    """Async context manager returned by AsyncDatabase.as_readonly()."""

    def __init__(self, async_db: "AsyncDatabase") -> None:
        self._async_db = async_db
        self._prev: bool = False

    async def __aenter__(self) -> "AsyncDatabase":
        self._prev = self._async_db._db._readonly
        self._async_db._db._readonly = True
        return self._async_db

    async def __aexit__(self, *_) -> bool:
        self._async_db._db._readonly = self._prev
        return False


class AsyncDatabase:
    """asyncio-compatible wrapper around Database.

    All blocking database operations are offloaded to the event loop's default
    thread-pool executor so they never stall the event loop.

    Usage::
        db = AsyncDatabase(":memory:")
        cur = await db.execute("SELECT 1 AS n")
        rows = await cur.fetchall()
        await db.close()

    As a context manager::
        async with AsyncDatabase(":memory:") as db:
            await db.execute("CREATE TABLE t (id INTEGER)")
    """

    def __init__(self, path, *, readonly: bool = False) -> None:
        from .database import Database
        self._db = Database(path, readonly=readonly)

    # ── Queries ────────────────────────────────────────────────────────────────

    async def execute(self, sql: str, params=None,
                      timeout_ms: int | None = None,
                      max_rows: int | None = None) -> AsyncCursor:
        loop = asyncio.get_running_loop()
        cur = await loop.run_in_executor(
            None,
            lambda: self._db.execute(sql, params, timeout_ms=timeout_ms, max_rows=max_rows),
        )
        return AsyncCursor(cur)

    async def executemany(self, sql: str, params_seq) -> AsyncCursor:
        loop = asyncio.get_running_loop()
        cur = await loop.run_in_executor(
            None,
            lambda: self._db.executemany(sql, params_seq),
        )
        return AsyncCursor(cur)

    async def executescript(self, sql: str) -> AsyncCursor:
        loop = asyncio.get_running_loop()
        cur = await loop.run_in_executor(
            None,
            lambda: self._db.executescript(sql),
        )
        return AsyncCursor(cur)

    # ── Transactions ───────────────────────────────────────────────────────────

    async def begin(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._db.begin)

    async def commit(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._db.commit)

    async def rollback(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._db.rollback)

    async def savepoint(self, name: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._db.savepoint(name))

    async def release_savepoint(self, name: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._db.release_savepoint(name))

    async def rollback_to_savepoint(self, name: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._db.rollback_to_savepoint(name))

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def in_transaction(self) -> bool:
        return self._db.in_transaction

    @property
    def readonly(self) -> bool:
        return self._db.readonly

    @readonly.setter
    def readonly(self, value: bool) -> None:
        self._db.readonly = value

    @property
    def max_rows(self) -> int | None:
        return self._db.max_rows

    @max_rows.setter
    def max_rows(self, value: int | None) -> None:
        self._db.max_rows = value

    @property
    def tables(self):
        return self._db.tables

    @property
    def indexes(self):
        return self._db.indexes

    @property
    def views(self):
        return self._db.views

    # ── Application-defined functions ──────────────────────────────────────────

    def create_function(self, name: str, n_args: int, fn) -> None:
        self._db.create_function(name, n_args, fn)

    def create_aggregate(self, name: str, n_args: int, aggregate_class) -> None:
        self._db.create_aggregate(name, n_args, aggregate_class)

    def set_authorizer(self, fn) -> None:
        self._db.set_authorizer(fn)

    # ── Read-only context manager ──────────────────────────────────────────────

    def as_readonly(self) -> _AsyncReadOnlyContext:
        """Async context manager: enforce read-only mode for the duration of the block."""
        return _AsyncReadOnlyContext(self)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._db.close)

    async def __aenter__(self) -> "AsyncDatabase":
        if not self._db.in_transaction:
            await self.begin()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            if self._db.in_transaction:
                await self.commit()
        else:
            if self._db.in_transaction:
                await self.rollback()
        return False
