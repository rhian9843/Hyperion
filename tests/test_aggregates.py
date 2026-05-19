# test suite for Hyperion
import os
import tempfile
import unittest
from subprocess import PIPE, run

DATABASE_COMMAND = ["python3", "-m", "hyperion"]

CREATE_USERS = "CREATE TABLE users (id INTEGER, name VARCHAR(32), email VARCHAR(255))"


def db_run(commands, db_path):
    """Run a list of SQL commands against a database file and return stdout lines."""
    result = run(
        DATABASE_COMMAND + [db_path],
        input="\n".join(commands) + "\n",
        stdout=PIPE,
        stderr=PIPE,
        encoding="utf-8",
    )
    lines = []
    for line in result.stdout.splitlines():
        stripped = line.removeprefix("H > ").strip()
        if stripped:
            lines.append(stripped)
    return result.returncode, lines


class TempDB:
    """Context manager that provides a fresh temporary database path."""
    def __enter__(self):
        self._f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._f.close()
        os.unlink(self._f.name)
        return self._f.name

    def __exit__(self, *_):
        try:
            os.unlink(self._f.name)
        except FileNotFoundError:
            pass


class TestAggregates(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_count_star(self):
        _, lines = db_run(["SELECT COUNT(*) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))

    def test_count_col_excludes_nulls(self):
        db_run(["INSERT INTO users (id, name) VALUES (4, dave)", ".exit"], self.db)
        _, lines = db_run(["SELECT COUNT(email) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))

    def test_min(self):
        _, lines = db_run(["SELECT MIN(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("1" in l for l in lines))

    def test_max(self):
        _, lines = db_run(["SELECT MAX(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))

    def test_sum(self):
        _, lines = db_run(["SELECT SUM(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("6" in l for l in lines))

    def test_avg(self):
        _, lines = db_run(["SELECT AVG(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("2" in l for l in lines))

    def test_multi_agg(self):
        _, lines = db_run(["SELECT COUNT(*), MIN(id), MAX(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))
        self.assertTrue(any("1" in l for l in lines))

    def test_agg_with_where(self):
        _, lines = db_run(["SELECT COUNT(*) FROM users WHERE id > 1", ".exit"], self.db)
        self.assertTrue(any("2" in l for l in lines))

    def test_agg_empty_table(self):
        db_run(["CREATE TABLE empty (x INTEGER)", ".exit"], self.db)
        _, lines = db_run(["SELECT COUNT(*) FROM empty", ".exit"], self.db)
        self.assertTrue(any("0" in l for l in lines))


class TestGroupBy(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE orders (dept VARCHAR(32), amount INTEGER)",
            "INSERT INTO orders VALUES (eng, 100)",
            "INSERT INTO orders VALUES (eng, 200)",
            "INSERT INTO orders VALUES (hr, 50)",
            "INSERT INTO orders VALUES (hr, 75)",
            "INSERT INTO orders VALUES (eng, 300)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_group_by_count(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders GROUP BY dept", ".exit"], self.db
        )
        # eng has 3 rows, hr has 2
        self.assertTrue(any("3" in l for l in lines))
        self.assertTrue(any("2" in l for l in lines))

    def test_group_by_sum(self):
        _, lines = db_run(
            ["SELECT dept, SUM(amount) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("600" in l for l in lines))   # eng: 100+200+300
        self.assertTrue(any("125" in l for l in lines))   # hr: 50+75

    def test_group_by_avg(self):
        _, lines = db_run(
            ["SELECT dept, AVG(amount) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("200" in l for l in lines))   # eng avg

    def test_group_by_min_max(self):
        _, lines = db_run(
            ["SELECT dept, MIN(amount), MAX(amount) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("100" in l for l in lines))
        self.assertTrue(any("300" in l for l in lines))

    def test_having_filters_groups(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders GROUP BY dept HAVING COUNT(*) > 2", ".exit"],
            self.db
        )
        self.assertTrue(any("eng" in l for l in lines))
        self.assertFalse(any("hr" in l for l in lines))

    def test_having_sum(self):
        _, lines = db_run(
            ["SELECT dept, SUM(amount) FROM orders GROUP BY dept HAVING SUM(amount) >= 600",
             ".exit"], self.db
        )
        self.assertTrue(any("eng" in l for l in lines))
        self.assertFalse(any("hr" in l for l in lines))

    def test_group_by_with_where(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders WHERE amount > 60 GROUP BY dept", ".exit"],
            self.db
        )
        # hr only has 75 > 60, so count=1; eng has 100,200,300 so count=3
        self.assertTrue(any("1" in l for l in lines))
        self.assertTrue(any("3" in l for l in lines))

    def test_group_by_order_by(self):
        _, lines = db_run(
            ["SELECT dept, SUM(amount) FROM orders GROUP BY dept ORDER BY dept ASC", ".exit"],
            self.db
        )
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_group_by_two_rows_returned(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("(2 rows)" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
