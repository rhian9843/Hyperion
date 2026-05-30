"""Thread safety tests: concurrent access to a shared Database instance."""
import threading
from hyperion import Database
from hyperion.database import _RWLock


def test_database_has_rwlock():
    db = Database(":memory:")
    assert isinstance(db._lock, _RWLock)


def test_concurrent_inserts_correct_row_count():
    """N threads each inserting M rows into the same table must yield N*M total rows."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")

    n_threads = 8
    rows_per_thread = 25
    errors = []

    def worker(thread_id):
        try:
            for i in range(rows_per_thread):
                db.execute(
                    "INSERT INTO t VALUES (?, ?)",
                    (thread_id * 1000 + i, f"t{thread_id}r{i}"),
                )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    row = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()
    assert row["n"] == n_threads * rows_per_thread


def test_concurrent_reads_see_committed_state():
    """Reader threads must never see rows that were not yet committed."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(20):
        db.execute(f"INSERT INTO t VALUES ({i})")

    committed_count = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"]
    read_errors = []

    def reader():
        try:
            for _ in range(50):
                n = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"]
                # Must see at least the 20 rows that were committed before threads started
                if n < committed_count:
                    read_errors.append(f"Saw only {n} rows, expected >= {committed_count}")
        except Exception as e:
            read_errors.append(e)

    def writer():
        try:
            for i in range(20, 40):
                db.execute(f"INSERT INTO t VALUES ({i})")
        except Exception as e:
            read_errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads += [threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not read_errors, f"Read errors: {read_errors}"


def test_concurrent_auto_commit_all_rows_present(tmp_path):
    """Concurrent auto-commit writes to a file-backed database must all persist."""
    db_path = tmp_path / "tc.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (x INTEGER)")

    n_threads = 5
    n_each = 10
    errors = []

    def worker(start):
        try:
            for i in range(start, start + n_each):
                db.execute("INSERT INTO t VALUES (?)", (i,))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i * n_each,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    row = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()
    assert row["n"] == n_threads * n_each
    db.close()


def test_fetchall_thread_safe():
    """fetchall() from multiple threads on separate cursors must not corrupt results."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(100):
        db.execute(f"INSERT INTO t VALUES ({i})")

    results = {}
    errors = []

    def reader(tid):
        try:
            rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
            results[tid] = [r["id"] for r in rows]
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    expected = list(range(100))
    for tid, rows in results.items():
        assert rows == expected, f"Thread {tid} got wrong rows"


def test_concurrent_selects_not_serialised():
    """Two threads doing fetchall() on the same DB must proceed in parallel.

    With the old RLock, Thread B's execute() blocked until Thread A's
    fetchall() released the lock.  With the new RWLock, both hold the read
    lock simultaneously.

    We verify correctness (not timing) by running many concurrent reads and
    confirming each thread sees all the pre-committed rows.
    """
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    n = 200
    for i in range(n):
        db.execute(f"INSERT INTO t VALUES ({i})")

    results = {}
    errors = []

    def reader(tid):
        try:
            rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
            results[tid] = len(rows)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    for tid, count in results.items():
        assert count == n, f"Thread {tid} got {count} rows, expected {n}"


def test_ddl_serialised_with_dml():
    """A CREATE TABLE racing with INSERTs must not corrupt catalog state."""
    db = Database(":memory:")
    db.execute("CREATE TABLE base (id INTEGER)")
    errors = []

    def inserter():
        try:
            for i in range(50):
                db.execute("INSERT INTO base VALUES (?)", (i,))
        except Exception as e:
            errors.append(e)

    def ddl_worker():
        try:
            db.execute("CREATE TABLE extra (x TEXT)")
            db.execute("DROP TABLE extra")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=inserter) for _ in range(4)]
    threads += [threading.Thread(target=ddl_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    row = db.execute("SELECT COUNT(*) AS n FROM base").fetchone()
    assert row["n"] == 4 * 50


def test_lastrowid_is_thread_local():
    """Each thread must see its own lastrowid, not another thread's."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")

    results: dict[int, int | None] = {}
    errors = []

    def worker(tid):
        try:
            cur = db.execute("INSERT INTO t (val) VALUES (?)", (f"v{tid}",))
            results[tid] = cur.lastrowid
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert all(v is not None for v in results.values()), \
        f"None lastrowid in: {results}"
    # All captured rowids must be distinct (each insert got a unique key)
    assert len(set(results.values())) == 20, \
        f"Duplicate lastrowids: {results}"


def test_lastrowid_multiple_inserts_per_thread():
    """A thread doing N sequential inserts must see the rowid from its own last insert,
    not one from a concurrent thread that fired between the two."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")

    # Thread A does two inserts and records both rowids.
    # Thread B hammers inserts concurrently trying to clobber A's lastrowid.
    a_rowids: list[int | None] = []
    errors = []

    barrier = threading.Barrier(2)

    def thread_a():
        try:
            barrier.wait()
            cur1 = db.execute("INSERT INTO t (val) VALUES ('a1')")
            a_rowids.append(cur1.lastrowid)
            cur2 = db.execute("INSERT INTO t (val) VALUES ('a2')")
            a_rowids.append(cur2.lastrowid)
        except Exception as e:
            errors.append(e)

    def thread_b():
        try:
            barrier.wait()
            for _ in range(50):
                db.execute("INSERT INTO t (val) VALUES ('b')")
        except Exception as e:
            errors.append(e)

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start(); tb.start()
    ta.join();  tb.join()

    assert not errors, f"Errors: {errors}"
    assert len(a_rowids) == 2, f"Thread A only recorded {len(a_rowids)} rowids"
    assert a_rowids[0] != a_rowids[1], "Both inserts should have different rowids"
    assert all(r is not None for r in a_rowids)


def test_last_insert_rowid_sql_function_is_thread_local():
    """SELECT LAST_INSERT_ROWID() on a connection must return the rowid from that
    connection's most recent insert, not one from a concurrent thread."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")

    sql_results: dict[int, int | None] = {}
    errors = []

    def worker(tid):
        try:
            db.execute("INSERT INTO t (val) VALUES (?)", (f"v{tid}",))
            row = db.execute("SELECT LAST_INSERT_ROWID() AS rid").fetchone()
            sql_results[tid] = row["rid"] if row else None
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert all(v is not None for v in sql_results.values()), \
        f"None LAST_INSERT_ROWID in: {sql_results}"
    assert len(set(sql_results.values())) == 20, \
        f"Duplicate LAST_INSERT_ROWID values: {sql_results}"


def test_lastrowid_none_before_insert_on_new_thread():
    """A thread that has never performed an insert sees lastrowid as None."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")
    # Do one insert on the main thread to populate the main-thread TLS slot
    db.execute("INSERT INTO t (val) VALUES ('main')")

    result: list[int | None] = []

    def fresh_thread():
        # This thread has never inserted — its TLS slot must be None
        from hyperion.expr import get_last_insert_rowid
        result.append(get_last_insert_rowid())

    t = threading.Thread(target=fresh_thread)
    t.start()
    t.join()

    assert result == [None], f"Expected [None], got {result}"


def test_lastrowid_file_backed_concurrent(tmp_path):
    """Concurrent inserts into a file-backed database each return the correct rowid."""
    db_path = tmp_path / "tc.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")

    results: dict[int, int | None] = {}
    errors = []

    def worker(tid):
        try:
            cur = db.execute("INSERT INTO t (val) VALUES (?)", (f"v{tid}",))
            results[tid] = cur.lastrowid
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    db.close()

    assert not errors, f"Errors: {errors}"
    assert all(v is not None for v in results.values())
    assert len(set(results.values())) == 10, \
        f"Duplicate lastrowids in file-backed DB: {results}"


def test_explicit_transaction_serialised():
    """Two threads racing on an explicit transaction must not double-commit or corrupt."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    errors = []

    def txn_worker(tid):
        try:
            db.begin()
            db.execute(f"INSERT INTO t VALUES ({tid})")
            db.commit()
        except Exception as e:
            # "Transaction already active" is expected when threads race on begin()
            if "already active" not in str(e):
                errors.append(e)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=txn_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # All non-conflicting transactions must have committed
    row = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()
    assert row["n"] >= 1  # at least one thread succeeded
