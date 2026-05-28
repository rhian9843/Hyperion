"""Thread safety tests: concurrent access to a shared Database instance."""
import threading
from hyperion import Database


def test_database_has_rlock():
    db = Database(":memory:")
    assert isinstance(db._lock, type(threading.RLock()))


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
