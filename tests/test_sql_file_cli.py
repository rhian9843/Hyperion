"""Tests for CLI .sql file execution: python -m hyperion <db> <script.sql>"""
import subprocess
import sys
import tempfile
import os
import pytest


def _run(db_path: str, sql_file: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hyperion", db_path, sql_file],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def sql_file():
    files = []

    def _make(content: str) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False)
        f.write(content)
        f.close()
        files.append(f.name)
        return f.name

    yield _make
    for path in files:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Basic execution ───────────────────────────────────────────────────────────

def test_ddl_and_select(sql_file):
    path = sql_file("""
        CREATE TABLE t (id INTEGER, name TEXT);
        INSERT INTO t VALUES (1, 'Alice');
        INSERT INTO t VALUES (2, 'Bob');
        SELECT * FROM t;
    """)
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert "Alice" in r.stdout
    assert "Bob" in r.stdout


def test_no_output_for_dml_only(sql_file):
    path = sql_file("""
        CREATE TABLE t (id INTEGER);
        INSERT INTO t VALUES (1);
    """)
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_multiple_selects(sql_file):
    path = sql_file("""
        CREATE TABLE t (x INTEGER);
        INSERT INTO t VALUES (10);
        INSERT INTO t VALUES (20);
        SELECT x FROM t WHERE x = 10;
        SELECT x FROM t WHERE x = 20;
    """)
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert "10" in r.stdout
    assert "20" in r.stdout


def test_empty_sql_file(sql_file):
    path = sql_file("   \n  \n  ")
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_comments_only_file(sql_file):
    path = sql_file("-- just a comment\n")
    r = _run(":memory:", path)
    assert r.returncode == 0


# ── Error handling ────────────────────────────────────────────────────────────

def test_syntax_error_prints_to_stderr_and_continues(sql_file):
    """Per-statement errors print to stderr but do not abort the script."""
    path = sql_file("SELECT FROM; SELECT 1 AS v;")
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert r.stderr.strip() != ""   # error message printed
    assert "1" in r.stdout           # subsequent statement still ran


def test_missing_table_prints_error_continues(sql_file):
    """SELECT from a missing table prints an error; script continues."""
    path = sql_file("SELECT * FROM nonexistent_table; SELECT 42 AS v;")
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert r.stderr.strip() != ""


def test_file_not_found_exits_nonzero():
    r = _run(":memory:", "/tmp/does_not_exist_hyperion.sql")
    assert r.returncode == 1
    assert "Error" in r.stderr


def test_error_continues_execution(sql_file):
    """SQLite compat: errors print and script continues; all statements run."""
    path = sql_file("""
        CREATE TABLE t (id INTEGER);
        SELECT * FROM nonexistent;
        INSERT INTO t VALUES (1);
    """)
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert r.stderr.strip() != ""   # the SELECT error was reported


# ── Persistence (file-backed db) ─────────────────────────────────────────────

def test_changes_persist_to_file(sql_file, tmp_path):
    db_path = str(tmp_path / "test.hdb")

    setup = sql_file("""
        CREATE TABLE t (id INTEGER, val TEXT);
        INSERT INTO t VALUES (1, 'hello');
    """)
    r = _run(db_path, setup)
    assert r.returncode == 0

    query = sql_file("SELECT * FROM t;")
    r = _run(db_path, query)
    assert r.returncode == 0
    assert "hello" in r.stdout


# ── Transaction in script ─────────────────────────────────────────────────────

def test_explicit_transaction_in_file(sql_file):
    path = sql_file("""
        CREATE TABLE t (id INTEGER);
        BEGIN;
        INSERT INTO t VALUES (42);
        COMMIT;
        SELECT * FROM t;
    """)
    r = _run(":memory:", path)
    assert r.returncode == 0
    assert "42" in r.stdout
