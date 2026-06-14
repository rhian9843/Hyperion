import contextlib
import json
import os
import struct
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .cursor import Cursor

from .constants import (PAGE_SIZE, PAGE_CKSUM_SZ, ROW_CELL_SIZE, ROW_INLINE_CAP,
                        PAGE_OVERFLOW, OVERFLOW_HDR, OVERFLOW_DATA_SZ)
from .errors import (NoSuchTableError, SchemaError, TransactionError)
from .btree import BTree
from .catalog import Catalog, TableMeta, IndexMeta, PublicationMeta, SubscriptionMeta, PolicyMeta, EventMeta
from .pager import Pager, MemoryPager
from .encoding import _idx_key_sz
from .constraints import ConstraintsMixin
from .ddl import DDLMixin
from .dml import DMLMixin
from .query import QueryMixin

# Page-0 header: [next_schema_pn: 4][schema_chunk_len: 4][ops_pn: 4][magic: 4]
_CAT0_HDR    = 16
_CAT0_MAGIC  = 0xCAFEBABE           # distinguishes split format from old format
_CAT0_CHUNK  = PAGE_SIZE - _CAT0_HDR - PAGE_CKSUM_SZ  # 4076

# Extra schema/ops pages share the same compact header
_CAT_HDR     = 8                    # [next_pn: 4][chunk_len: 4]
_CAT_CHUNK   = PAGE_SIZE - _CAT_HDR - PAGE_CKSUM_SZ    # 4084


class _RWLock:
    """Single-writer / multiple-reader lock with reentrant write support.

    Multiple threads may hold the read lock simultaneously.  A write request
    blocks until all readers finish, then grants exclusive access.  A thread
    that already holds the write lock may re-acquire it without deadlocking
    (reentrant write, tracked by depth counter).  Pending writers are counted
    so new readers wait once a writer is queued, preventing writer starvation.
    """

    __slots__ = ("_cond", "_readers", "_write_owner", "_write_depth",
                 "_writers_waiting")

    def __init__(self) -> None:
        self._cond             = threading.Condition(threading.Lock())
        self._readers: int     = 0
        self._write_owner      = None   # thread ident or None
        self._write_depth: int = 0
        self._writers_waiting: int = 0

    @contextlib.contextmanager
    def read(self):
        """Shared (read) lock — multiple holders allowed, blocks on active/pending writers."""
        tid = threading.get_ident()
        acquired = False
        with self._cond:
            if self._write_owner == tid:
                pass  # current write owner also has implicit read access
            else:
                while self._write_owner is not None or self._writers_waiting > 0:
                    self._cond.wait()
                self._readers += 1
                acquired = True
        try:
            yield
        finally:
            if acquired:
                with self._cond:
                    self._readers -= 1
                    if self._readers == 0:
                        self._cond.notify_all()

    @contextlib.contextmanager
    def write(self):
        """Exclusive (write) lock — reentrant for the same thread."""
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()

    def acquire_write(self) -> None:
        """Acquire the write lock without a context manager (pair with release_write)."""
        tid = threading.get_ident()
        with self._cond:
            if self._write_owner == tid:
                self._write_depth += 1
            else:
                self._writers_waiting += 1
                while self._readers > 0 or self._write_owner is not None:
                    self._cond.wait()
                self._writers_waiting -= 1
                self._write_owner = tid
                self._write_depth = 1

    def release_write(self) -> None:
        """Release one depth level of the write lock."""
        with self._cond:
            self._write_depth -= 1
            if self._write_depth == 0:
                self._write_owner = None
                self._cond.notify_all()


class _ReadOnlyContext:
    __slots__ = ("_db", "_prev")

    def __init__(self, db: "Database") -> None:
        self._db = db

    def __enter__(self) -> "Database":
        self._prev = self._db._readonly
        self._db._readonly = True
        return self._db

    def __exit__(self, *_) -> bool:
        self._db._readonly = self._prev
        return False


class Database(DDLMixin, DMLMixin, QueryMixin, ConstraintsMixin):
    def __init__(self, path: "Path | str", *, readonly: bool = False):
        if str(path) == ":memory:":
            self._pager: Pager | MemoryPager = MemoryPager()
        else:
            self._pager = Pager(Path(path), readonly=readonly)
        self._readonly = readonly
        self.max_rows: int | None = None
        (self._catalog,
         self._catalog_extra,
         self._catalog_ops_pn,
         self._catalog_ops_extra) = self._load_catalog()
        self._txn_depth      = 0
        # Each entry: (name, pages_snap, dirty_set, cat_bytes, cat_extra,
        #              ops_pn, ops_extra)
        self._savepoints: list[tuple] = []
        self.fk_enforcement  = True
        self.row_factory     = None   # callable(cursor, row_dict) -> Any; None = dict
        self._authorizer     = None   # callable(action, table, col, db, trigger) -> int
        self._plan_cache: OrderedDict[str, dict] = OrderedDict()  # LRU: SQL → AST
        self._plan_cache_lock = threading.Lock()  # guards all _plan_cache mutations
        # Schema bytes cache: skip page writes when structure hasn't changed.
        self._schema_flushed_bytes: bytes = self._catalog.schema_to_bytes()
        # Ops snapshot: detect which tables/indexes changed since last flush so
        # ops_to_bytes() only re-serializes touched entries (O(dirty) not O(all)).
        self._ops_snap_tables:  dict = {}   # {name: (root_page, next_page, next_key)}
        self._ops_snap_indexes: dict = {}   # {name: (root_page, next_page)}
        self._ops_snap_global:  tuple = (
            self._catalog.next_free_page, tuple(self._catalog.free_pages)
        )
        # Readers-writer lock: concurrent SELECTs share the read lock; writes
        # (DML, DDL, transactions) require exclusive access.  Write lock is
        # reentrant for the same thread so nested calls (e.g. executescript →
        # commit, close → begin/drop/commit) don't deadlock.
        self._lock = _RWLock()
        self._for_update_held = False  # True while this txn holds a FOR UPDATE write lock
        self._isolation_level: str = "READ COMMITTED"  # current isolation level
        self._isolation_held  = False  # True when isolation level holds a write lock
        self._txn_start_time: float | None = None  # monotonic time when BEGIN was called
        self._user_funcs: dict = {}  # name.upper() → (n_args, callable)
        self._user_aggs:  dict = {}  # name.upper() → (n_args, aggregate_class)
        self._changelog     = None   # lazily-created changelog view
        self._sub_workers: dict = {}    # sub_name → SubscriptionWorker
        # Physical replication state — stored in <db>.phys_state JSON (not catalog)
        self._phys_subs_path: Path | None = (
            Path(path).with_suffix(".phys_state")
            if str(path) != ":memory:" else None
        )
        self._phys_subs: dict = {}     # name → PhysicalSubscriptionMeta
        self._phys_workers: dict = {}  # name → PhysicalReplicationWorker
        self._load_phys_subs()
        self._current_user_id: int | None = None
        self._is_superuser: bool = False
        self._event_scheduler = None
        # Crash recovery: re-stage any logical entries recovered from the WAL back
        # into a fresh WAL transaction.  This preserves retention (entries with
        # lsn > min_consumed_lsn survive subsequent checkpoints) without needing
        # a separate archive file.
        if not isinstance(self._pager, MemoryPager) and self._pager._recovery_logical:
            self._restage_recovery_logical()
        # Restart subscription workers for any subscriptions already in catalog
        for sub_name in list(self._catalog.subscriptions):
            self._start_sub_worker(sub_name)
        # Restart physical replication workers marked auto_start
        for sub_name, sub in list(self._phys_subs.items()):
            if sub.auto_start:
                self._start_phys_worker(sub_name)
        # Start event scheduler if any events are defined
        self._start_event_scheduler()

    def _exec_stmt_with_ctes(self, stmt: dict, ctes: dict) -> list[dict]:
        from .executor import _rows_for_stmt
        return _rows_for_stmt(stmt, self, ctes)

    # ── Read-only toggle ──────────────────────────────────────────────────────

    @property
    def readonly(self) -> bool:
        return self._readonly

    @readonly.setter
    def readonly(self, value: bool) -> None:
        self._readonly = value

    def as_readonly(self):
        """Context manager: enforce read-only mode for the duration of the block.

        Restores the previous readonly state on exit regardless of exceptions.

        Usage::
            with db.as_readonly():
                agent.query(db)   # only SELECT allowed here
            db.execute("INSERT ...")  # writes allowed again
        """
        return _ReadOnlyContext(self)

    # ── Transaction control ────────────────────────────────────────────────────

    @property
    def in_transaction(self) -> bool:
        return self._txn_depth > 0

    def begin(self) -> None:
        import time as _time
        # REPEATABLE READ and SERIALIZABLE: acquire write lock before BEGIN so
        # no other writer can interleave; lock is held until commit/rollback.
        needs_excl = self._isolation_level in ("REPEATABLE READ", "SERIALIZABLE")
        if needs_excl and not self._isolation_held and not self._for_update_held:
            self._lock.acquire_write()
            self._isolation_held = True
        with self._lock.write():
            if self._txn_depth > 0:
                raise TransactionError("Transaction already active")
            self._pager.begin()
            # If the pager found pending WAL frames and updated _cache, reload
            # the catalog so this transaction starts with up-to-date next_key /
            # root pages from a previous connection's lazy-checkpointed commit.
            if getattr(self._pager, '_wal_had_pending', False):
                self._reload_catalog()
            self._txn_depth = 1
            self._txn_start_time = _time.time()

    def commit(self) -> None:
        with self._lock.write():
            if self._txn_depth == 0:
                raise TransactionError("No active transaction")
            self._flush_catalog()
            self._pager.commit(min_consumed_lsn=self._min_consumed_lsn(),
                               catalog_lsn=1)  # non-zero → track phys_dirty
            self._txn_depth = 0
            self._txn_start_time = None
        if self._for_update_held:
            self._for_update_held = False
            self._lock.release_write()
        if self._isolation_held:
            self._isolation_held = False
            self._lock.release_write()

    def rollback(self) -> None:
        with self._lock.write():
            if self._txn_depth == 0:
                raise TransactionError("No active transaction")
            self._savepoints.clear()
            self._pager.rollback()
            self._reload_catalog()
            self._txn_depth = 0
            self._txn_start_time = None
        if self._for_update_held:
            self._for_update_held = False
            self._lock.release_write()
        if self._isolation_held:
            self._isolation_held = False
            self._lock.release_write()

    def set_isolation_level(self, level: str) -> None:
        """Set the transaction isolation level for subsequent transactions.

        Must be called outside an active transaction.
        Levels: READ UNCOMMITTED, READ COMMITTED (default),
                REPEATABLE READ, SERIALIZABLE.
        READ UNCOMMITTED is treated as READ COMMITTED (Hyperion cannot expose
        uncommitted pages from other connections).
        REPEATABLE READ and SERIALIZABLE both acquire an exclusive write lock
        for the full transaction duration.
        """
        if self._txn_depth > 0:
            raise TransactionError(
                "Cannot change isolation level inside an active transaction"
            )
        valid = {"READ UNCOMMITTED", "READ COMMITTED",
                 "REPEATABLE READ", "SERIALIZABLE"}
        if level not in valid:
            raise ValueError(f"Unknown isolation level: '{level}'")
        self._isolation_level = level

    def _acquire_for_update(self) -> None:
        """Escalate the current transaction to an exclusive write lock.

        Called when executing SELECT ... FOR UPDATE.  The write lock is held
        until the transaction ends (commit or rollback), blocking any other
        thread from acquiring the write lock (and thereby blocking concurrent
        INSERTs, UPDATEs, DELETEs) until this transaction commits or rolls back.
        """
        if not self._for_update_held:
            self._lock.acquire_write()
            self._for_update_held = True

    # ── Savepoints ─────────────────────────────────────────────────────────────

    def savepoint(self, name: str) -> None:
        with self._lock.write():
            if self._txn_depth == 0:
                self._pager.begin()
                self._txn_depth = 1
            pages_snap = {n: bytes(self._pager._working[n])
                          for n in self._pager._dirty if n in self._pager._working}
            dirty_snap  = set(self._pager._dirty)
            cat_snap    = self._catalog.snap()
            cat_extra   = list(self._catalog_extra)
            ops_pn      = self._catalog_ops_pn
            ops_extra   = list(self._catalog_ops_extra)
            self._savepoints.append(
                (name, pages_snap, dirty_snap, cat_snap, cat_extra, ops_pn, ops_extra))

    def release_savepoint(self, name: str) -> None:
        with self._lock.write():
            idx = self._find_savepoint(name)
            del self._savepoints[idx:]

    def rollback_to_savepoint(self, name: str) -> None:
        with self._lock.write():
            idx = self._find_savepoint(name)
            _, pages_snap, dirty_snap, cat_snap, cat_extra, ops_pn, ops_extra = \
                self._savepoints[idx]
            del self._savepoints[idx + 1:]  # keep this savepoint alive (SQLite behaviour)
            # Evict pages added after the savepoint
            for pn in set(self._pager._dirty) - dirty_snap:
                self._pager._working.pop(pn, None)
            # Restore snapshotted working pages to savepoint state
            for pn, content in pages_snap.items():
                self._pager._working[pn] = bytearray(content)
            self._pager._dirty = set(dirty_snap)
            # Restore catalog and page-chain metadata
            self._catalog.restore_snap(cat_snap)
            self._catalog_extra     = list(cat_extra)
            self._catalog_ops_pn    = ops_pn
            self._catalog_ops_extra = list(ops_extra)
            # Invalidate schema cache so the next commit forces a full schema write.
            self._schema_flushed_bytes = b""
            # restore_snap() cleared the snippet caches but left _ops_snap_tables
            # intact.  If a table's (root_page, next_page, next_key) matches the
            # stale snapshot, _flush_ops would skip re-serializing it, producing
            # an ops page with empty table_ops.  Clear both ops snapshots so
            # _flush_ops unconditionally re-encodes every table/index on the
            # next commit.
            self._ops_snap_tables.clear()
            self._ops_snap_indexes.clear()

    # ── Application-defined functions ──────────────────────────────────────────

    def create_function(self, name: str, n_args: int, fn) -> None:
        """Register a custom scalar function callable from SQL.

        Args:
            name:   SQL function name (case-insensitive).
            n_args: Number of expected arguments, or -1 for variadic.
            fn:     Callable invoked with evaluated SQL arguments.
        """
        with self._lock.write():
            self._user_funcs[name.upper()] = (n_args, fn)

    def create_aggregate(self, name: str, n_args: int, aggregate_class) -> None:
        """Register a custom aggregate function callable from SQL GROUP BY.

        aggregate_class must implement:
            __init__(self)      — called once per group
            step(self, *args)   — called once per row in the group
            finalize(self)      — called after all rows; returns the result

        Args:
            name:            SQL function name (case-insensitive).
            n_args:          Number of per-row arguments, or -1 for variadic.
            aggregate_class: Class implementing the aggregate protocol.
        """
        with self._lock.write():
            self._user_aggs[name.upper()] = (n_args, aggregate_class)

    # ── Schema semantic metadata ──────────────────────────────────────────────

    def set_meta(self, object_type: str, object_name: str, key: str,
                 value: str) -> None:
        """Attach a semantic tag to any catalog object.

        Args:
            object_type: Kind of object — e.g. ``"table"``, ``"column"``,
                         ``"index"``, ``"view"``.  Any string is accepted.
            object_name: Identifier of the object.  Use ``"table.column"``
                         style for columns (e.g. ``"users.email"``).
            key:         Tag name, e.g. ``"description"``, ``"embedding_model"``.
            value:       Tag value string.
        """
        with self._lock.write():
            m = self._catalog.meta
            if object_type not in m:
                m[object_type] = {}
            if object_name not in m[object_type]:
                m[object_type][object_name] = {}
            m[object_type][object_name][key] = value

    def get_meta(self, object_type: str, object_name: str,
                 key: str | None = None):
        """Retrieve semantic tags for a catalog object.

        Returns the value string when *key* is given, or a ``{key: value}``
        dict when *key* is ``None``.  Returns ``None`` / ``{}`` when nothing
        is stored.
        """
        with self._lock.read():
            by_name = self._catalog.meta.get(object_type, {})
            tags = by_name.get(object_name, {})
            if key is not None:
                return tags.get(key)
            return dict(tags)

    def delete_meta(self, object_type: str, object_name: str,
                    key: str | None = None) -> int:
        """Remove semantic tags for a catalog object.

        When *key* is given, removes only that tag.  When *key* is ``None``,
        removes all tags for the ``(object_type, object_name)`` pair.
        Returns the number of entries removed.
        """
        with self._lock.write():
            m = self._catalog.meta
            by_name = m.get(object_type, {})
            tags = by_name.get(object_name, {})
            if key is not None:
                if key in tags:
                    del tags[key]
                    if not tags:
                        del by_name[object_name]
                    if not by_name:
                        del m[object_type]
                    return 1
                return 0
            n = len(tags)
            if n:
                del by_name[object_name]
                if not by_name:
                    del m[object_type]
            return n

    # ── PEP 249 DB-API ────────────────────────────────────────────────────────

    def cursor(self) -> "Cursor":
        from .cursor import Cursor
        return Cursor(self)

    def execute(self, sql: str, params=None, timeout_ms: int | None = None,
               max_rows: int | None = None) -> "Cursor":
        return self.cursor().execute(sql, params, timeout_ms=timeout_ms, max_rows=max_rows)

    def executemany(self, sql: str, params_seq) -> "Cursor":
        return self.cursor().executemany(sql, params_seq)

    def executescript(self, sql: str) -> "Cursor":
        return self.cursor().executescript(sql)

    def set_authorizer(self, fn) -> None:
        """Register an authorizer callback invoked before each SQL operation.

        fn(action_code, table, column, db_name, trigger_name) -> int
        Return SQLITE_OK (0) to allow, SQLITE_DENY (1) to raise an error,
        or SQLITE_IGNORE (2) to silently skip the operation.
        Pass None to remove the authorizer.
        """
        with self._lock.write():
            self._authorizer = fn

    def iterdump(self):
        """Yield SQL statements that recreate the full database (like sqlite3.iterdump)."""
        with self._lock.read():
            lines = list(self._iterdump_inner())
        yield from lines

    def _iterdump_inner(self):
        from .schema import deserialize_row
        from .introspect import _schema_to_sql, _trigger_to_sql
        from .cursor import _sql_literal

        yield "BEGIN TRANSACTION;"

        for tname, tmeta in self._catalog.tables.items():
            yield _schema_to_sql(tname, tmeta.schema, tmeta.temporary) + ";"
            if tmeta.storage_type == "column":
                row_iter = (row for _, row in self._table_btree(tmeta).scan_rows())
            else:
                row_iter = (deserialize_row(tmeta.schema, self._unpack_row_cell(raw))
                            for _, raw in self._table_btree(tmeta).scan())
            for row in row_iter:
                cols = [c.name for c in tmeta.schema.columns if not c.is_generated]
                vals = ", ".join(_sql_literal(row.get(c)) for c in cols)
                yield f"INSERT INTO \"{tname}\" VALUES ({vals});"

        for iname, imeta in self._catalog.indexes.items():
            if iname.startswith("_pk_"):
                continue  # auto-created by CREATE TABLE PRIMARY KEY
            cols = ", ".join(imeta.columns)
            yield f"CREATE INDEX \"{iname}\" ON \"{imeta.table_name}\" ({cols});"

        for vname, vsql in self._catalog.views.items():
            yield f"CREATE VIEW \"{vname}\" AS {vsql};"

        for trig_name, tmeta in self._catalog.triggers.items():
            yield _trigger_to_sql(trig_name, tmeta) + ";"

        yield "COMMIT;"

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "Database":
        if not self.in_transaction:
            self.begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            if self.in_transaction:
                self.commit()
        else:
            if self.in_transaction:
                self.rollback()
        return False

    def _find_savepoint(self, name: str) -> int:
        for i in range(len(self._savepoints) - 1, -1, -1):
            if self._savepoints[i][0] == name:
                return i
        raise TransactionError(f"No such savepoint: '{name}'")

    def _load_catalog(self) -> "tuple[Catalog, list[int], int, list[int]]":
        """Load catalog from pages.  Returns (catalog, schema_extras, ops_pn, ops_extras).

        Detects old (combined-JSON) vs. new (split schema+ops) on-disk format via
        a magic constant at bytes 12-15 of page 0.
        """
        page0 = self._pager.read_page(Catalog.CATALOG_PAGE)
        magic = struct.unpack_from("I", page0, 12)[0]

        if magic == _CAT0_MAGIC:
            return self._load_catalog_split(page0)
        else:
            return self._load_catalog_legacy()

    def _load_catalog_split(self, page0: bytearray) \
            -> "tuple[Catalog, list[int], int, list[int]]":
        """Load new split-format catalog from page 0."""
        next_schema_pn = struct.unpack_from("I", page0, 0)[0]
        schema_len     = struct.unpack_from("I", page0, 4)[0]
        ops_pn         = struct.unpack_from("I", page0, 8)[0]

        # Schema chain
        schema_data    = bytes(page0[_CAT0_HDR: _CAT0_HDR + schema_len])
        schema_extras: list[int] = []
        pn = next_schema_pn
        while pn:
            page      = self._pager.read_page(pn)
            next_pn   = struct.unpack_from("I", page, 0)[0]
            chunk_len = struct.unpack_from("I", page, 4)[0]
            if chunk_len:
                schema_data += bytes(page[_CAT_HDR: _CAT_HDR + chunk_len])
            schema_extras.append(pn)
            pn = next_pn

        # Ops chain
        ops_data    = b""
        ops_extras: list[int] = []
        if ops_pn:
            pn = ops_pn
            first = True
            while pn:
                page      = self._pager.read_page(pn)
                next_pn   = struct.unpack_from("I", page, 0)[0]
                chunk_len = struct.unpack_from("I", page, 4)[0]
                if chunk_len:
                    ops_data += bytes(page[_CAT_HDR: _CAT_HDR + chunk_len])
                if not first:
                    ops_extras.append(pn)
                first = False
                pn = next_pn

        cat = Catalog.from_schema_and_ops_bytes(schema_data, ops_data)
        return cat, schema_extras, ops_pn, ops_extras

    def _load_catalog_legacy(self) -> "tuple[Catalog, list[int], int, list[int]]":
        """Load old combined-JSON catalog (backward compatibility)."""
        data: bytes = b""
        extras: list[int] = []
        pn = Catalog.CATALOG_PAGE
        while True:
            page      = self._pager.read_page(pn)
            next_pn   = struct.unpack_from("I", page, 0)[0]
            chunk_len = struct.unpack_from("I", page, 4)[0]
            if chunk_len:
                data += bytes(page[_CAT_HDR: _CAT_HDR + chunk_len])
            if next_pn == 0:
                break
            extras.append(next_pn)
            pn = next_pn
        return Catalog.from_bytes(data), extras, 0, []

    def _reload_catalog(self) -> None:
        (self._catalog,
         self._catalog_extra,
         self._catalog_ops_pn,
         self._catalog_ops_extra) = self._load_catalog()
        self._schema_flushed_bytes = self._catalog.schema_to_bytes()
        # Reset ops snapshot so next flush rebuilds all snippets from scratch.
        self._ops_snap_tables.clear()
        self._ops_snap_indexes.clear()
        self._ops_snap_global = (
            self._catalog.next_free_page, tuple(self._catalog.free_pages)
        )

    # ── Catalog flush — schema and ops written independently ──────────────────

    def _flush_catalog(self) -> None:
        """Flush catalog to pages.

        Schema pages: written only when structural definitions changed (DDL).
        Ops pages: written on every commit — small regardless of schema size.
        """
        # Ensure ops page is allocated before writing page 0 (schema flush
        # writes page 0, which must carry the correct ops_pn).
        if self._catalog_ops_pn == 0:
            self._catalog_ops_pn = self._alloc_page()

        new_schema = self._catalog.schema_to_bytes()
        if new_schema != self._schema_flushed_bytes:
            self._flush_schema(new_schema)
            self._schema_flushed_bytes = new_schema
        else:
            # Schema unchanged; still need to refresh page 0 when ops_pn was
            # just allocated above (its value changed from 0).
            if self._pager._in_txn:
                page0 = self._pager._working.get(Catalog.CATALOG_PAGE)
                if page0 is None or struct.unpack_from("I", page0, 8)[0] != self._catalog_ops_pn:
                    self._write_page0_header()

        # Always flush ops (cheap — proportional to n_tables, not schema depth)
        self._flush_ops()

    def _write_page0_header(self) -> None:
        """Write page 0's 16-byte header without touching the schema JSON chunk."""
        page           = self._pager.get_page(Catalog.CATALOG_PAGE)
        next_schema_pn = self._catalog_extra[0] if self._catalog_extra else 0
        # Preserve the existing schema chunk length in the header
        schema_len = struct.unpack_from("I", page, 4)[0]
        struct.pack_into("I", page, 0, next_schema_pn)
        struct.pack_into("I", page, 4, schema_len)
        struct.pack_into("I", page, 8, self._catalog_ops_pn)
        struct.pack_into("I", page, 12, _CAT0_MAGIC)
        self._pager.flush(Catalog.CATALOG_PAGE)

    def _flush_schema(self, payload: bytes) -> None:
        """Write schema JSON to the schema page chain (page 0 + schema_extras)."""
        for _ in range(4):
            # Page 0 holds _CAT0_CHUNK bytes; extra pages hold _CAT_CHUNK each
            n_needed = 1 + max(0, (len(payload) - _CAT0_CHUNK + _CAT_CHUNK - 1) // _CAT_CHUNK)
            n_have   = 1 + len(self._catalog_extra)
            if n_needed == n_have:
                break
            if n_needed > n_have:
                for _ in range(n_needed - n_have):
                    self._catalog_extra.append(self._alloc_page())
            else:
                freed = self._catalog_extra[n_needed - 1:]
                self._catalog_extra = self._catalog_extra[:n_needed - 1]
                for pn in freed:
                    self._free_page(pn)
            payload = self._catalog.schema_to_bytes()  # recompute after alloc/free

        # Build chunk list: first chunk is _CAT0_CHUNK, rest are _CAT_CHUNK
        chunks: list[bytes] = []
        if payload:
            chunks.append(payload[:_CAT0_CHUNK])
            rest = payload[_CAT0_CHUNK:]
            chunks += [rest[i: i + _CAT_CHUNK]
                       for i in range(0, len(rest), _CAT_CHUNK)]
        else:
            chunks = [b""]

        all_pns = [Catalog.CATALOG_PAGE] + self._catalog_extra
        while len(chunks) < len(all_pns):
            chunks.append(b"")

        for i, (pn, chunk) in enumerate(zip(all_pns, chunks)):
            page    = self._pager.get_page(pn)
            next_pn = all_pns[i + 1] if i + 1 < len(all_pns) else 0
            if pn == Catalog.CATALOG_PAGE:
                # 16-byte header on page 0
                struct.pack_into("I", page, 0,  next_pn)
                struct.pack_into("I", page, 4,  len(chunk))
                struct.pack_into("I", page, 8,  self._catalog_ops_pn)
                struct.pack_into("I", page, 12, _CAT0_MAGIC)
                page[_CAT0_HDR: _CAT0_HDR + len(chunk)] = chunk
                page[_CAT0_HDR + len(chunk):]            = bytearray(PAGE_SIZE - _CAT0_HDR - len(chunk))
            else:
                # 8-byte header on extra schema pages
                struct.pack_into("I", page, 0, next_pn)
                struct.pack_into("I", page, 4, len(chunk))
                page[_CAT_HDR: _CAT_HDR + len(chunk)] = chunk
                page[_CAT_HDR + len(chunk):]           = bytearray(PAGE_SIZE - _CAT_HDR - len(chunk))
            self._pager.flush(pn)

    def _flush_ops(self) -> None:
        """Write operational-state JSON to the ops page chain.

        Before serializing, compare current ops values against the last-flush
        snapshot to mark only changed tables/indexes as dirty.  ops_to_bytes()
        then re-serializes only dirty entries, keeping cost O(dirty) instead
        of O(all_tables) per commit.
        """
        cat = self._catalog
        for name, m in cat.tables.items():
            if m.temporary:
                continue
            curr = (m.root_page, m.next_page, m.next_key)
            if self._ops_snap_tables.get(name) != curr:
                cat.mark_table_ops_dirty(name)
                self._ops_snap_tables[name] = curr
        # Remove entries for dropped tables
        for name in list(self._ops_snap_tables):
            if name not in cat.tables:
                cat._t_snippets.pop(name, None)
                del self._ops_snap_tables[name]

        for name, m in cat.indexes.items():
            curr = (m.root_page, m.next_page)
            if self._ops_snap_indexes.get(name) != curr:
                cat.mark_index_ops_dirty(name)
                self._ops_snap_indexes[name] = curr
        for name in list(self._ops_snap_indexes):
            if name not in cat.indexes:
                cat._i_snippets.pop(name, None)
                del self._ops_snap_indexes[name]

        global_curr = (cat.next_free_page, tuple(cat.free_pages))
        if self._ops_snap_global != global_curr:
            cat.mark_global_ops_dirty()
            self._ops_snap_global = global_curr

        payload = cat.ops_to_bytes()
        for _ in range(4):
            n_needed = max(1, (len(payload) + _CAT_CHUNK - 1) // _CAT_CHUNK)
            n_have   = 1 + len(self._catalog_ops_extra)
            if n_needed == n_have:
                break
            if n_needed > n_have:
                for _ in range(n_needed - n_have):
                    self._catalog_ops_extra.append(self._alloc_page())
            else:
                freed = self._catalog_ops_extra[n_needed - 1:]
                self._catalog_ops_extra = self._catalog_ops_extra[:n_needed - 1]
                for pn in freed:
                    self._free_page(pn)
            payload = self._catalog.ops_to_bytes()

        all_pns = [self._catalog_ops_pn] + self._catalog_ops_extra
        chunks  = [payload[i: i + _CAT_CHUNK]
                   for i in range(0, len(payload), _CAT_CHUNK)]
        while len(chunks) < len(all_pns):
            chunks.append(b"")

        for i, (pn, chunk) in enumerate(zip(all_pns, chunks)):
            page    = self._pager.get_page(pn)
            next_pn = all_pns[i + 1] if i + 1 < len(all_pns) else 0
            struct.pack_into("I", page, 0, next_pn)
            struct.pack_into("I", page, 4, len(chunk))
            page[_CAT_HDR: _CAT_HDR + len(chunk)] = chunk
            page[_CAT_HDR + len(chunk):]           = bytearray(PAGE_SIZE - _CAT_HDR - len(chunk))
            self._pager.flush(pn)

    # ── Internal helpers (used by all mixins via self) ─────────────────────────

    def _meta(self, name: str) -> TableMeta:
        if name not in self._catalog.tables:
            raise NoSuchTableError(f"No such table: '{name}'")
        return self._catalog.tables[name]

    def _alloc_page(self) -> int:
        if self._catalog.free_pages:
            return self._catalog.free_pages.pop()
        pn = self._catalog.next_free_page
        self._catalog.next_free_page += 1
        return pn

    def _free_page(self, pn: int) -> None:
        self._catalog.free_pages.append(pn)

    def _collect_column_store_pages(self, root: int) -> list[int]:
        """Return every page belonging to a column store table rooted at root."""
        from .column_store import (PAGE_COLUMN_HDR, PAGE_COLUMN_CHUNK,
                                   HDR_PREFIX_SZ, HDR_ENTRY_SZ)
        pages: list[int] = [root]
        hdr = self._pager.read_page(root)
        if hdr[0] != PAGE_COLUMN_HDR:
            return pages
        num_cols = struct.unpack_from('<H', hdr, 5)[0]
        visited: set[int] = set()
        from .errors import CorruptPageError
        for i in range(num_cols):
            off = HDR_PREFIX_SZ + i * HDR_ENTRY_SZ
            first_chunk = struct.unpack_from('<I', hdr, off)[0]
            pn = first_chunk
            while pn and pn not in visited:
                visited.add(pn)
                pages.append(pn)
                try:
                    chunk = self._pager.read_page(pn)
                except CorruptPageError:
                    break  # page already in list; checksum scan will report it
                pn = struct.unpack_from('<I', chunk, 1)[0]
        return pages

    def _collect_tree_pages(self, root: int, *, key_sz: int = 8) -> list[int]:
        int_cell = key_sz + BTree.CHILD_SZ
        pages: list[int] = []
        visited: set[int] = set()
        stack = [root]
        while stack:
            pn = stack.pop()
            if pn == 0 or pn in visited:
                continue
            visited.add(pn)
            pages.append(pn)
            page = self._pager.get_page(pn)
            n_cells = struct.unpack_from("I", page, 6)[0]
            sibling  = struct.unpack_from("I", page, 10)[0]
            if page[0] == BTree.NODE_INTERNAL:
                stack.append(sibling)
                for i in range(n_cells):
                    rc = struct.unpack_from("I", page,
                                           BTree.HDR + i * int_cell + key_sz)[0]
                    stack.append(rc)
            else:
                if sibling:
                    stack.append(sibling)
        return pages

    def _table_btree(self, meta: TableMeta):
        if meta.storage_type == "column":
            from .column_store import ColumnStore
            return ColumnStore(self._pager, meta.root_page, meta.schema,
                               self._make_alloc(meta), self._free_page)
        return BTree(self._pager, meta.root_page, ROW_CELL_SIZE,
                     self._make_alloc(meta))

    def _index_btree(self, idx: IndexMeta) -> BTree:
        return BTree(self._pager, idx.root_page, 8, self._make_idx_alloc(idx),
                     key_sz=_idx_key_sz(len(idx.columns)))

    def _make_alloc(self, meta: TableMeta) -> Callable[[], int]:
        def alloc() -> int:
            pn = self._alloc_page()
            meta.next_page = pn + 1
            return pn
        return alloc

    def _make_idx_alloc(self, idx: IndexMeta) -> Callable[[], int]:
        def alloc() -> int:
            pn = self._alloc_page()
            idx.next_page = pn + 1
            return pn
        return alloc

    # ── Overflow page management ──────────────────────────────────────────────

    def _write_overflow(self, data: bytes) -> int:
        """Write data to a linked overflow page chain. Returns first page number."""
        first_page = 0
        prev_pn: int | None = None
        offset = 0
        while offset < len(data) or first_page == 0:
            pn = self._alloc_page()
            if first_page == 0:
                first_page = pn
            if prev_pn is not None:
                pg = self._pager.get_page(prev_pn)
                struct.pack_into("I", pg, 1, pn)
            chunk = data[offset: offset + OVERFLOW_DATA_SZ]
            pg = self._pager.get_page(pn)
            pg[0] = PAGE_OVERFLOW
            struct.pack_into("I", pg, 1, 0)             # next page = 0 (last for now)
            struct.pack_into("I", pg, 5, len(chunk))
            pg[OVERFLOW_HDR: OVERFLOW_HDR + len(chunk)] = chunk
            prev_pn = pn
            offset += OVERFLOW_DATA_SZ
            if offset >= len(data):
                break
        return first_page

    def _read_overflow(self, first_page: int, total_len: int) -> bytes:
        """Reassemble data from an overflow page chain."""
        result = bytearray()
        pn = first_page
        while pn and len(result) < total_len:
            pg       = self._pager.read_page(pn)
            data_len = struct.unpack_from("I", pg, 5)[0]
            result  += pg[OVERFLOW_HDR: OVERFLOW_HDR + data_len]
            pn       = struct.unpack_from("I", pg, 1)[0]
        return bytes(result[:total_len])

    def _free_overflow(self, first_page: int) -> None:
        """Free all pages in an overflow chain."""
        pn = first_page
        while pn:
            pg  = self._pager.read_page(pn)
            nxt = struct.unpack_from("I", pg, 1)[0]
            self._free_page(pn)
            pn  = nxt

    def _pack_row_cell(self, varlen: bytes) -> bytes:
        """Wrap variable-length row bytes into a fixed ROW_CELL_SIZE B-tree cell."""
        cell = bytearray(ROW_CELL_SIZE)
        if len(varlen) <= ROW_INLINE_CAP:
            cell[0] = 0                                  # inline
            struct.pack_into("I", cell, 1, len(varlen))
            struct.pack_into("I", cell, 5, 0)
            cell[9: 9 + len(varlen)] = varlen
        else:
            first_page = self._write_overflow(varlen)
            cell[0] = 1                                  # overflow
            struct.pack_into("I", cell, 1, len(varlen))
            struct.pack_into("I", cell, 5, first_page)
        return bytes(cell)

    def _unpack_row_cell(self, cell: bytes) -> bytes:
        """Extract variable-length row bytes from a ROW_CELL_SIZE B-tree cell."""
        is_overflow = cell[0]
        total_len   = struct.unpack_from("I", cell, 1)[0]
        if not is_overflow:
            return bytes(cell[9: 9 + total_len])
        first_page = struct.unpack_from("I", cell, 5)[0]
        return self._read_overflow(first_page, total_len)

    def _cell_is_overflow(self, cell: bytes) -> bool:
        return bool(cell[0])

    def _indexes_for(self, table: str) -> list[IndexMeta]:
        return [m for m in self._catalog.indexes.values()
                if m.table_name == table]

    @property
    def tables(self) -> dict[str, TableMeta]:
        return self._catalog.tables

    @property
    def indexes(self) -> dict[str, IndexMeta]:
        return self._catalog.indexes

    @property
    def views(self) -> dict[str, str]:
        return self._catalog.views

    def create_view(self, name: str, sql: str,
                    if_not_exists: bool = False,
                    or_replace: bool = False) -> None:
        with self._lock.write():
            if name in self._catalog.views:
                if or_replace:
                    pass  # overwrite below
                elif if_not_exists:
                    return
                else:
                    raise SchemaError(f"View '{name}' already exists")
            self._catalog.views[name] = sql

    def drop_view(self, name: str, if_exists: bool = False) -> None:
        with self._lock.write():
            if name not in self._catalog.views:
                if if_exists:
                    return
                raise NoSuchTableError(f"No such view: '{name}'")
            del self._catalog.views[name]

    # ── Logical replication ───────────────────────────────────────────────────

    @property
    def changelog(self):
        """Changelog view backed by the WAL (on-disk) or an in-memory log (:memory:).

        On-disk: WALBackedChangelog reads WAL._committed_logical — the single
        in-memory mirror of all retained LOGICAL frames.  No archive file exists.
        In-memory: InMemoryChangelog used by tests and the HTTP test suite.
        """
        if self._changelog is None:
            if isinstance(self._pager, MemoryPager):
                from .changelog import InMemoryChangelog
                self._changelog = InMemoryChangelog()
            else:
                from .changelog import WALBackedChangelog
                self._changelog = WALBackedChangelog(self._pager)
        return self._changelog

    def _is_published(self, table: str) -> bool:
        """True if table is covered by at least one publication."""
        for pub in self._catalog.publications.values():
            if not pub.tables or table in pub.tables:
                return True
        return False

    def _append_changelog(self, table: str, op: str, row: dict,
                           row_before: dict | None = None) -> None:
        """Record a DML event for logical replication if the table is published.

        For on-disk databases the record is staged into the WAL and committed
        atomically with the page writes — no separate I/O or crash window.
        For :memory: databases (no WAL) it is appended directly to the in-memory
        changelog.
        """
        if not self._catalog.publications:
            return
        if not self._is_published(table):
            return
        import time as _t
        from .changelog import ChangelogEntry
        self._catalog.lsn += 1
        self._catalog.mark_global_ops_dirty()
        entry = ChangelogEntry(
            lsn=self._catalog.lsn, table=table, op=op,
            row=row, row_before=row_before, ts=_t.time(),
        )
        if isinstance(self._pager, MemoryPager):
            self.changelog.append(entry)
        else:
            # Stage in WAL — written atomically with page frames on commit.
            if self._pager._wal is not None:
                self._pager._wal.stage_logical(entry.to_dict())

    def _min_consumed_lsn(self) -> int:
        """Minimum LSN that has been confirmed by all active subscribers.

        Passed to pager.commit/close so the WAL checkpoint knows how far back
        to retain LOGICAL frames.  Returns catalog.lsn (i.e. retain nothing)
        when there are no subscriptions — no subscriber means no retention needed.
        """
        subs = self._catalog.subscriptions
        if not subs:
            return self._catalog.lsn
        return min(s.last_lsn for s in subs.values())

    def _restage_recovery_logical(self) -> None:
        """Re-stage recovered logical entries into a fresh WAL after crash recovery.

        replay_if_exists extracts LOGICAL frames from a crash-surviving WAL,
        applies PAGE frames to the db file, and deletes the WAL.  The recovered
        logical entries live in pager._recovery_logical.  We re-stage them by
        opening a WAL transaction, staging each entry, and committing with no
        page changes — so they appear in _committed_logical and survive the next
        checkpoint at the appropriate min_consumed_lsn.
        """
        entries = self._pager._recovery_logical
        if not entries:
            return
        self._pager.begin()
        for entry in entries:
            self._pager._wal.stage_logical(entry)
        self._pager.commit(min_consumed_lsn=self._min_consumed_lsn())
        self._pager._recovery_logical = []

    def create_publication(self, name: str, tables: list[str],
                           if_not_exists: bool = False) -> None:
        with self._lock.write():
            if name in self._catalog.publications:
                if if_not_exists:
                    return
                from .errors import SchemaError
                raise SchemaError(f"Publication '{name}' already exists")
            self._catalog.publications[name] = PublicationMeta(name, list(tables))

    def drop_publication(self, name: str, if_exists: bool = False) -> None:
        with self._lock.write():
            if name not in self._catalog.publications:
                if if_exists:
                    return
                from .errors import SchemaError
                raise SchemaError(f"No such publication: '{name}'")
            del self._catalog.publications[name]

    def create_subscription(self, name: str, connection: str,
                             publication: str,
                             if_not_exists: bool = False) -> None:
        with self._lock.write():
            if name in self._catalog.subscriptions:
                if if_not_exists:
                    return
                from .errors import SchemaError
                raise SchemaError(f"Subscription '{name}' already exists")
            self._catalog.subscriptions[name] = SubscriptionMeta(
                name, connection, publication)
        self._start_sub_worker(name)

    def drop_subscription(self, name: str, if_exists: bool = False) -> None:
        self._stop_sub_worker(name)
        with self._lock.write():
            if name not in self._catalog.subscriptions:
                if if_exists:
                    return
                from .errors import SchemaError
                raise SchemaError(f"No such subscription: '{name}'")
            del self._catalog.subscriptions[name]

    def _start_sub_worker(self, sub_name: str) -> None:
        if sub_name in self._sub_workers:
            return
        from .replication import SubscriptionWorker
        w = SubscriptionWorker(self, sub_name)
        self._sub_workers[sub_name] = w
        w.start()

    def _stop_sub_worker(self, sub_name: str) -> None:
        w = self._sub_workers.pop(sub_name, None)
        if w is not None:
            w.stop()

    # ── Physical replication ──────────────────────────────────────────────────

    def _load_phys_subs(self) -> None:
        from .physical_replication import PhysicalSubscriptionMeta
        self._phys_subs = {}
        if self._phys_subs_path is None or not self._phys_subs_path.exists():
            return
        try:
            entries = json.loads(self._phys_subs_path.read_text())
            for entry in entries:
                m = PhysicalSubscriptionMeta(**entry)
                self._phys_subs[m.name] = m
        except Exception:
            pass

    def _save_phys_subs(self) -> None:
        if self._phys_subs_path is None:
            return
        from dataclasses import asdict
        data = [asdict(m) for m in self._phys_subs.values()]
        self._phys_subs_path.write_text(json.dumps(data, indent=2))

    def create_physical_subscription(self, name: str, connection: str,
                                     if_not_exists: bool = False) -> None:
        with self._lock.write():
            if name in self._phys_subs:
                if if_not_exists:
                    return
                from .errors import SchemaError
                raise SchemaError(f"Physical subscription '{name}' already exists")
            from .physical_replication import PhysicalSubscriptionMeta
            self._phys_subs[name] = PhysicalSubscriptionMeta(name, connection)
            self._save_phys_subs()

    def drop_physical_subscription(self, name: str, if_exists: bool = False) -> None:
        self._stop_phys_worker(name)
        with self._lock.write():
            if name not in self._phys_subs:
                if if_exists:
                    return
                from .errors import SchemaError
                raise SchemaError(f"No such physical subscription: '{name}'")
            del self._phys_subs[name]
            self._save_phys_subs()

    def _start_phys_worker(self, sub_name: str) -> None:
        if sub_name in self._phys_workers:
            return
        from .physical_replication import PhysicalReplicationWorker
        w = PhysicalReplicationWorker(self, sub_name)
        self._phys_workers[sub_name] = w
        w.start()

    def _stop_phys_worker(self, sub_name: str) -> None:
        w = self._phys_workers.pop(sub_name, None)
        if w is not None:
            w.stop()

    def start_slave(self, name: str | None = None) -> str:
        with self._lock.write():
            targets = [name] if name else list(self._phys_subs)
            for sub_name in targets:
                if sub_name not in self._phys_subs:
                    from .errors import SchemaError
                    raise SchemaError(f"No such physical subscription: '{sub_name}'")
                self._phys_subs[sub_name].auto_start = True
                self._start_phys_worker(sub_name)
            self._save_phys_subs()
        return "Slave started."

    def stop_slave(self, name: str | None = None) -> str:
        targets = [name] if name else list(self._phys_workers)
        for sub_name in list(targets):
            self._stop_phys_worker(sub_name)
            if sub_name in self._phys_subs:
                self._phys_subs[sub_name].auto_start = False
        self._save_phys_subs()
        self._readonly = False  # promote: re-enable writes regardless of worker state
        return "Slave stopped."

    def show_master_status(self) -> list[dict]:
        if isinstance(self._pager, MemoryPager):
            return [{"binlog_pos": 0, "db_size": 0, "wal_size": 0}]
        wal_path = self._pager._path.with_suffix(".wal")
        return [{
            "binlog_pos": self._pager._phys_current_lsn,
            "db_size":    os.path.getsize(self._pager._path),
            "wal_size":   os.path.getsize(wal_path) if wal_path.exists() else 0,
        }]

    def show_slave_status(self) -> list[dict]:
        rows = []
        for name, sub in self._phys_subs.items():
            worker = self._phys_workers.get(name)
            rows.append({
                "name":       name,
                "connection": sub.connection,
                "last_lsn":   sub.last_lsn,
                "status":     worker.status if worker else "Stopped",
                "last_error": worker._last_error if worker else "",
                "last_sync":  round(worker._last_sync_ts, 3) if worker else 0.0,
            })
        return rows

    def show_binlog(self) -> list[dict]:
        if isinstance(self._pager, MemoryPager):
            return []
        phys_dirty = dict(self._pager._phys_dirty)
        return [
            {"page_num": pn, "catalog_lsn": lsn}
            for pn, (lsn, _) in sorted(phys_dirty.items(), key=lambda x: x[1][0])
        ]

    def close(self) -> None:
        if self._event_scheduler is not None:
            self._event_scheduler.stop()
            self._event_scheduler = None
        for name in list(self._phys_workers):
            self._stop_phys_worker(name)
        for name in list(self._sub_workers):
            self._stop_sub_worker(name)
        with self._lock.write():
            temp_tables = [n for n, m in self._catalog.tables.items() if m.temporary]
            if temp_tables:
                self.begin()
                for name in temp_tables:
                    self.drop_table(name)
                self.commit()
            self._pager.close(min_consumed_lsn=self._min_consumed_lsn())

    def vacuum(self) -> str:
        """Rebuild the database file compactly, reclaiming space from deleted rows."""
        with self._lock.write():
            return self._vacuum_inner()

    def _vacuum_inner(self) -> str:
        import tempfile, shutil
        from .schema import deserialize_row

        if isinstance(self._pager, MemoryPager):
            return "Database vacuumed."

        if self._txn_depth > 0:
            raise TransactionError("Cannot VACUUM inside a transaction")

        path = self._pager._path

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        tmp_path.unlink(missing_ok=True)

        import copy
        new_db = Database(tmp_path)
        new_db.begin()
        for tname, tmeta in list(self._catalog.tables.items()):
            new_db.create_table(tmeta.schema,
                                storage_type=tmeta.storage_type)
            if tmeta.storage_type == "column":
                for _, row in self._table_btree(tmeta).scan_rows():
                    new_db.insert(tname, row)
            else:
                for _, raw in self._table_btree(tmeta).scan():
                    row = deserialize_row(tmeta.schema, self._unpack_row_cell(raw))
                    new_db.insert(tname, row)
        for idx_name, idx_meta in list(self._catalog.indexes.items()):
            if idx_name not in new_db._catalog.indexes:
                new_db.create_index(idx_name, idx_meta.table_name, idx_meta.columns)
        for vname, vsql in list(self._catalog.views.items()):
            new_db.create_view(vname, vsql)
        for trig_name, trig_meta in list(self._catalog.triggers.items()):
            new_db.create_trigger(trig_name, trig_meta)
        new_db._catalog.stats = copy.deepcopy(self._catalog.stats)
        new_db._catalog.mark_stats_dirty()
        new_db._catalog.meta  = copy.deepcopy(self._catalog.meta)
        new_db.commit()
        new_db._pager.close()

        self._pager.close()
        shutil.move(str(tmp_path), str(path))

        self._pager = Pager(path)
        (self._catalog,
         self._catalog_extra,
         self._catalog_ops_pn,
         self._catalog_ops_extra) = self._load_catalog()
        self._schema_flushed_bytes = self._catalog.schema_to_bytes()
        self._txn_depth = 0
        self._savepoints.clear()
        return "Database vacuumed."

    # ── Row-Level Security ────────────────────────────────────────────────────

    def set_user(self, user_id: int | None) -> None:
        self._current_user_id = user_id

    def set_superuser(self, flag: bool) -> None:
        self._is_superuser = flag

    def enable_rls(self, table: str) -> None:
        meta = self._meta(table)
        meta.rls_enabled = True
        self._catalog.mark_global_ops_dirty()
        self._schema_flushed_bytes = b""

    def disable_rls(self, table: str) -> None:
        meta = self._meta(table)
        meta.rls_enabled = False
        self._catalog.mark_global_ops_dirty()
        self._schema_flushed_bytes = b""

    def create_policy(self, name: str, table: str, using_expr: str,
                      if_not_exists: bool = False) -> None:
        self._meta(table)   # raises NoSuchTableError if table unknown
        if name in self._catalog.policies:
            if if_not_exists:
                return
            raise SchemaError(f"Policy '{name}' already exists")
        self._catalog.policies[name] = PolicyMeta(name, table, using_expr)
        self._schema_flushed_bytes = b""

    def drop_policy(self, name: str, table: str, if_exists: bool = False) -> None:
        if name not in self._catalog.policies:
            if if_exists:
                return
            raise SchemaError(f"No such policy: '{name}'")
        del self._catalog.policies[name]
        self._schema_flushed_bytes = b""

    def _rls_allowed(self, table: str, row: dict) -> bool:
        """Return True if row is visible under RLS for the current user."""
        meta = self._catalog.tables.get(table)
        if meta is None or not meta.rls_enabled or self._is_superuser:
            return True
        policies = [p for p in self._catalog.policies.values() if p.table == table]
        if not policies:
            return False
        from .expr import eval_expr
        for policy in policies:
            try:
                if eval_expr(policy.using_expr, row):
                    return True
            except Exception:
                pass
        return False

    # ── Event Scheduler ──────────────────────────────────────────────────────

    def _start_event_scheduler(self) -> None:
        if self._event_scheduler is not None:
            return
        from .event_scheduler import EventScheduler
        sched = EventScheduler(self)
        self._event_scheduler = sched
        sched.start()

    def create_event(self, name: str, schedule_type: str,
                     interval_seconds: int, at_time: str, sql: str,
                     if_not_exists: bool = False) -> None:
        if name in self._catalog.events:
            if if_not_exists:
                return
            raise SchemaError(f"Event '{name}' already exists")
        self._catalog.events[name] = EventMeta(
            name=name, schedule_type=schedule_type,
            interval_seconds=interval_seconds, at_time=at_time, sql=sql)
        self._schema_flushed_bytes = b""
        self._start_event_scheduler()

    def drop_event(self, name: str, if_exists: bool = False) -> None:
        if name not in self._catalog.events:
            if if_exists:
                return
            raise SchemaError(f"No such event: '{name}'")
        del self._catalog.events[name]
        self._schema_flushed_bytes = b""

    def enable_event(self, name: str) -> None:
        if name not in self._catalog.events:
            raise SchemaError(f"No such event: '{name}'")
        self._catalog.events[name].enabled = True
        self._schema_flushed_bytes = b""

    def disable_event(self, name: str) -> None:
        if name not in self._catalog.events:
            raise SchemaError(f"No such event: '{name}'")
        self._catalog.events[name].enabled = False
        self._schema_flushed_bytes = b""

    def show_events(self) -> list[dict]:
        rows = []
        for evt in self._catalog.events.values():
            if evt.schedule_type == "INTERVAL":
                schedule = f"EVERY {evt.interval_seconds} SECOND"
            else:
                schedule = f"AT {evt.at_time!r}"
            rows.append({
                "name":     evt.name,
                "schedule": schedule,
                "sql":      evt.sql,
                "enabled":  evt.enabled,
                "last_run": evt.last_run or None,
            })
        return rows
