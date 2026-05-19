import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

"""
test_package.py
---------------
Runs a comprehensive set of SQL operations through the new hyperion package
and verifies the output matches expected results.  These are independent of
test.py so you can run them without the full test suite.

Run:
    python test_package.py
"""

import sys
import tempfile
import traceback
from pathlib import Path
import hyperion

PASS  = "\033[92m✓\033[0m"
FAIL  = "\033[91m✗\033[0m"
total = 0
fails = 0


def run(label: str, sql: str, db, *, expect=None, expect_error=False):
    global total, fails
    total += 1
    try:
        ast    = hyperion.parse(sql)
        result = hyperion.execute(ast, db)
        if expect_error:
            print(f"  {FAIL} {label}  →  expected error but got: {result!r}")
            fails += 1
        elif expect is not None and expect not in result:
            print(f"  {FAIL} {label}  →  expected {expect!r} in {result!r}")
            fails += 1
        else:
            print(f"  {PASS} {label}")
    except Exception as e:
        if expect_error:
            print(f"  {PASS} {label}  (error as expected: {e})")
        else:
            print(f"  {FAIL} {label}  →  {e}")
            fails += 1


tmp = Path(tempfile.mktemp(suffix=".db"))
db  = hyperion.Database(tmp)

try:
    # ── DDL ───────────────────────────────────────────────────────────────────
    print("\n[ DDL ]")
    run("CREATE TABLE dept",
        "CREATE TABLE dept (id INTEGER, name TEXT)",
        db, expect="created")
    run("CREATE TABLE emp",
        "CREATE TABLE emp (id INTEGER, name TEXT, dept_id INTEGER, salary REAL)",
        db, expect="created")
    run("CREATE INDEX on emp(dept_id)",
        "CREATE INDEX idx_dept ON emp(dept_id)",
        db, expect="created")
    run("duplicate table raises error",
        "CREATE TABLE dept (id INTEGER)",
        db, expect_error=True)

    # ── INSERT ────────────────────────────────────────────────────────────────
    print("\n[ INSERT ]")
    for row in [
        "INSERT INTO dept VALUES (1, Engineering)",
        "INSERT INTO dept VALUES (2, Marketing)",
    ]:
        run(f"insert dept: {row[25:45]}...", row, db, expect="1 row inserted")
    for row in [
        "INSERT INTO emp VALUES (1, Alice, 1, 90000.0)",
        "INSERT INTO emp VALUES (2, Bob, 1, 80000.0)",
        "INSERT INTO emp VALUES (3, Carol, 2, 70000.0)",
        "INSERT INTO emp VALUES (4, Dave, 2, 60000.0)",
    ]:
        run(f"insert emp: {row[24:40]}...", row, db, expect="1 row inserted")

    # ── SELECT ────────────────────────────────────────────────────────────────
    print("\n[ SELECT ]")
    run("SELECT *",               "SELECT * FROM emp", db, expect="Alice")
    run("SELECT with WHERE =",    "SELECT * FROM emp WHERE id = 1", db, expect="Alice")
    run("SELECT with WHERE >",    "SELECT * FROM emp WHERE salary > 75000", db, expect="Alice")
    run("SELECT with WHERE LIKE", "SELECT * FROM emp WHERE name LIKE A%", db, expect="Alice")
    run("SELECT LIMIT",           "SELECT * FROM emp LIMIT 2", db, expect="Alice")
    run("SELECT ORDER BY DESC",   "SELECT * FROM emp ORDER BY salary DESC", db, expect="Alice")
    run("SELECT DISTINCT",        "SELECT DISTINCT dept_id FROM emp", db)
    run("SELECT IS NULL",         "SELECT * FROM emp WHERE salary IS NOT NULL", db, expect="Alice")

    # ── Aggregates ────────────────────────────────────────────────────────────
    print("\n[ Aggregates ]")
    run("COUNT(*)",               "SELECT COUNT(*) FROM emp", db, expect="4")
    run("AVG(salary)",            "SELECT AVG(salary) FROM emp", db, expect="75000")
    run("MAX(salary)",            "SELECT MAX(salary) FROM emp", db, expect="90000")
    run("MIN(salary)",            "SELECT MIN(salary) FROM emp", db, expect="60000")
    run("SUM(salary)",            "SELECT SUM(salary) FROM emp", db, expect="300000")
    run("GROUP BY + COUNT",
        "SELECT dept_id, COUNT(*) FROM emp GROUP BY dept_id",
        db, expect="2")
    run("GROUP BY + HAVING",
        "SELECT dept_id, AVG(salary) FROM emp GROUP BY dept_id HAVING AVG(salary) > 75000",
        db, expect="85000")

    # ── JOIN ──────────────────────────────────────────────────────────────────
    print("\n[ JOIN ]")
    run("INNER JOIN",
        "SELECT emp.name, dept.name FROM emp INNER JOIN dept ON emp.dept_id = dept.id",
        db, expect="Engineering")
    run("LEFT JOIN",
        "SELECT emp.name, dept.name FROM emp LEFT JOIN dept ON emp.dept_id = dept.id",
        db, expect="Alice")

    # ── UPDATE / DELETE ───────────────────────────────────────────────────────
    print("\n[ UPDATE / DELETE ]")
    run("UPDATE",  "UPDATE emp SET salary = 95000 WHERE id = 1", db, expect="1 row")
    run("verify update", "SELECT * FROM emp WHERE id = 1", db, expect="95000")
    run("DELETE",  "DELETE FROM emp WHERE id = 4", db, expect="1 row")
    run("verify delete count", "SELECT COUNT(*) FROM emp", db, expect="3")

    # ── Subqueries ────────────────────────────────────────────────────────────
    print("\n[ Subqueries ]")
    run("IN subquery",
        "SELECT name FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE name = Engineering)",
        db, expect="Alice")
    run("EXISTS subquery",
        "SELECT name FROM emp WHERE EXISTS (SELECT * FROM dept WHERE dept.id = emp.dept_id AND dept.name = Engineering)",
        db, expect="Alice")

    # ── Set operations ────────────────────────────────────────────────────────
    print("\n[ Set operations ]")
    run("UNION",
        "SELECT name FROM emp WHERE id = 1 UNION SELECT name FROM emp WHERE id = 2",
        db, expect="Alice")
    run("INTERSECT",
        "SELECT dept_id FROM emp WHERE id = 1 INTERSECT SELECT dept_id FROM emp WHERE id = 2",
        db, expect="1")

    # ── Transactions ──────────────────────────────────────────────────────────
    print("\n[ Transactions ]")
    run("BEGIN",    "BEGIN",    db, expect="started")
    run("ROLLBACK", "ROLLBACK", db, expect="rolled back")

    # ── ALTER TABLE ───────────────────────────────────────────────────────────
    print("\n[ ALTER TABLE ]")
    run("ADD COLUMN",
        "ALTER TABLE emp ADD COLUMN bonus REAL",
        db, expect="added")
    run("RENAME COLUMN",
        "ALTER TABLE emp RENAME COLUMN bonus TO commission",
        db, expect="renamed")
    run("DROP COLUMN",
        "ALTER TABLE emp DROP COLUMN commission",
        db, expect="dropped")

    # ── DROP ──────────────────────────────────────────────────────────────────
    print("\n[ DROP ]")
    run("DROP INDEX", "DROP INDEX idx_dept", db, expect="dropped")
    run("DROP TABLE", "DROP TABLE dept",     db, expect="dropped")

finally:
    db.close()
    tmp.unlink(missing_ok=True)
    tmp.with_suffix(".wal").unlink(missing_ok=True)

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'─' * 50}")
print(f"Ran {total} checks  |  {total - fails} passed  |  {fails} failed")
if fails == 0:
    print(f"{PASS} All checks passed.")
else:
    print(f"{FAIL} {fails} check(s) failed.")
    sys.exit(1)
