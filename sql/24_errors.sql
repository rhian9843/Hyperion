-- 24: Intentional error demonstrations — exhaustive coverage
-- Every error-triggering statement prints to stderr; execution continues.
-- Each section is self-contained: setup → error → verify outcome → teardown.
--
-- Error types covered:
--   DDL:         TableExistsError, NoSuchTableError, IndexExistsError,
--                NoSuchIndexError, ColumnExistsError, NoSuchColumnError, SchemaError
--   Constraints: UniqueConstraintError, NotNullConstraintError,
--                CheckConstraintError, ForeignKeyConstraintError
--   DML:         DataError (col/value mismatch), generated column write
--   Transactions:TransactionError (no active tx, bad savepoint)
--   References:  SELECT missing table, INSERT into view without trigger
--   Parse:       syntax error


-- ════════════════════════════════════════════════════════════════════════
-- DDL ERRORS
-- ════════════════════════════════════════════════════════════════════════

-- ── E01. CREATE TABLE — table already exists ──────────────────────────────────
CREATE TABLE e01 (id INTEGER);
-- Error: e01 already exists
CREATE TABLE e01 (id INTEGER);
SELECT 'E01 table exists' AS test, COUNT(*) AS cnt FROM e01;
DROP TABLE e01;

-- ── E02. DROP TABLE — table does not exist ────────────────────────────────────
-- Error: no table named no_such_table
DROP TABLE no_such_table;
SELECT 'E02 drop nonexistent table' AS test, 1 AS ok;

-- ── E03. TRUNCATE — table does not exist ──────────────────────────────────────
-- Error: no table named no_such_table
TRUNCATE TABLE no_such_table;
SELECT 'E03 truncate nonexistent table' AS test, 1 AS ok;

-- ── E04. CREATE INDEX — index already exists ──────────────────────────────────
CREATE TABLE e04 (id INTEGER, name TEXT);
CREATE INDEX ix_e04 ON e04(name);
-- Error: index ix_e04 already exists
CREATE INDEX ix_e04 ON e04(name);
SELECT 'E04 index already exists' AS test, 1 AS ok;
DROP TABLE e04;

-- ── E05. DROP INDEX — index does not exist ────────────────────────────────────
-- Error: no index named no_such_index
DROP INDEX no_such_index;
SELECT 'E05 drop nonexistent index' AS test, 1 AS ok;

-- ── E06. ALTER TABLE ADD COLUMN — column already exists ───────────────────────
CREATE TABLE e06 (id INTEGER, name TEXT);
-- Error: column 'name' already exists in e06
ALTER TABLE e06 ADD COLUMN name TEXT;
SELECT 'E06 add duplicate column' AS test, 1 AS ok;
DROP TABLE e06;

-- ── E07. ALTER TABLE RENAME COLUMN — column does not exist ────────────────────
CREATE TABLE e07 (id INTEGER);
-- Error: column 'x' not found in e07
ALTER TABLE e07 RENAME COLUMN x TO y;
SELECT 'E07 rename missing column' AS test, 1 AS ok;
DROP TABLE e07;

-- ── E08. ALTER TABLE DROP COLUMN — column does not exist ──────────────────────
CREATE TABLE e08 (id INTEGER);
-- Error: column 'x' not found in e08
ALTER TABLE e08 DROP COLUMN x;
SELECT 'E08 drop missing column' AS test, 1 AS ok;
DROP TABLE e08;

-- ── E09. CREATE VIEW — view already exists ────────────────────────────────────
CREATE TABLE e09 (id INTEGER);
CREATE VIEW v_e09 AS SELECT * FROM e09;
-- Error: view 'v_e09' already exists
CREATE VIEW v_e09 AS SELECT * FROM e09;
SELECT 'E09 view already exists' AS test, 1 AS ok;
DROP VIEW  v_e09;
DROP TABLE e09;

-- ── E10. DROP VIEW — view does not exist ─────────────────────────────────────
-- Error: no view named no_such_view
DROP VIEW no_such_view;
SELECT 'E10 drop nonexistent view' AS test, 1 AS ok;

-- ── E11. DROP TRIGGER — trigger does not exist ────────────────────────────────
-- Error: trigger 'no_such_trigger' does not exist
DROP TRIGGER no_such_trigger;
SELECT 'E11 drop nonexistent trigger' AS test, 1 AS ok;


-- ════════════════════════════════════════════════════════════════════════
-- CONSTRAINT ERRORS
-- ════════════════════════════════════════════════════════════════════════

-- ── E12. UNIQUE / PRIMARY KEY — duplicate single-column PK ───────────────────
CREATE TABLE e12 (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO e12 VALUES (1, 'Alice');
-- Error: UNIQUE constraint failed: e12.id
INSERT INTO e12 VALUES (1, 'Bob');
SELECT 'E12 dup pk' AS test, COUNT(*) AS cnt FROM e12;
-- Expected: 1
DROP TABLE e12;

-- ── E13. UNIQUE — duplicate value on UNIQUE column ───────────────────────────
CREATE TABLE e13 (id INTEGER PRIMARY KEY, email TEXT UNIQUE);
INSERT INTO e13 VALUES (1, 'alice@example.com');
-- Error: UNIQUE constraint failed: e13.email
INSERT INTO e13 VALUES (2, 'alice@example.com');
SELECT 'E13 dup unique col' AS test, COUNT(*) AS cnt FROM e13;
-- Expected: 1
DROP TABLE e13;

-- ── E14. UNIQUE — duplicate composite PRIMARY KEY ─────────────────────────────
CREATE TABLE e14_students (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE e14_courses  (id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE e14_enroll (
    student_id INTEGER NOT NULL,
    course_id  INTEGER NOT NULL,
    grade      TEXT,
    PRIMARY KEY (student_id, course_id),
    FOREIGN KEY (student_id) REFERENCES e14_students(id),
    FOREIGN KEY (course_id)  REFERENCES e14_courses(id)
);
INSERT INTO e14_students VALUES (1, 'Alice');
INSERT INTO e14_courses  VALUES (10, 'Math');
INSERT INTO e14_enroll   VALUES (1, 10, 'A');
-- Error: UNIQUE constraint failed: e14_enroll(student_id, course_id)
INSERT INTO e14_enroll VALUES (1, 10, 'duplicate');
SELECT 'E14 dup composite pk' AS test, COUNT(*) AS cnt FROM e14_enroll;
-- Expected: 1
DROP TABLE e14_enroll;
DROP TABLE e14_courses;
DROP TABLE e14_students;

-- ── E15. NOT NULL — INSERT NULL into NOT NULL column ─────────────────────────
CREATE TABLE e15 (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
-- Error: Column 'name' is NOT NULL
INSERT INTO e15 VALUES (1, NULL);
SELECT 'E15 insert null into not null' AS test, COUNT(*) AS cnt FROM e15;
-- Expected: 0
DROP TABLE e15;

-- ── E16. NOT NULL — UPDATE sets NOT NULL column to NULL ──────────────────────
CREATE TABLE e16 (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
INSERT INTO e16 VALUES (1, 'Alice');
-- Error: Column 'name' is NOT NULL
UPDATE e16 SET name = NULL WHERE id = 1;
SELECT 'E16 update not null to null' AS test, name FROM e16 WHERE id = 1;
-- Expected: Alice (update was rejected)
DROP TABLE e16;

-- ── E17. CHECK — INSERT violates CHECK constraint ────────────────────────────
CREATE TABLE e17 (
    id     INTEGER PRIMARY KEY,
    amount REAL    CHECK(amount > 0),
    level  TEXT    CHECK(level IN ('junior', 'mid', 'senior'))
);
-- Error: CHECK constraint failed: e17.amount CHECK (amount > 0)
INSERT INTO e17 VALUES (1, -100, 'junior');
-- Error: CHECK constraint failed: e17.level CHECK (level IN ...)
INSERT INTO e17 VALUES (2, 100, 'intern');
SELECT 'E17 check insert' AS test, COUNT(*) AS cnt FROM e17;
-- Expected: 0
DROP TABLE e17;

-- ── E18. CHECK — UPDATE violates CHECK constraint ────────────────────────────
CREATE TABLE e18 (id INTEGER PRIMARY KEY, amount REAL CHECK(amount > 0));
INSERT INTO e18 VALUES (1, 500);
-- Error: CHECK constraint failed: e18.amount CHECK (amount > 0)
UPDATE e18 SET amount = -1 WHERE id = 1;
SELECT 'E18 check update' AS test, amount FROM e18 WHERE id = 1;
-- Expected: 500 (update rejected)
DROP TABLE e18;

-- ── E19. FOREIGN KEY — INSERT child with non-existent parent ─────────────────
CREATE TABLE e19_parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE e19_child  (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES e19_parent(id)
);
INSERT INTO e19_parent VALUES (1, 'Engineering');
INSERT INTO e19_child  VALUES (1, 1);
-- Error: FK constraint failed — parent_id=99 does not exist
INSERT INTO e19_child VALUES (2, 99);
SELECT 'E19 fk insert bad parent' AS test, COUNT(*) AS cnt FROM e19_child;
-- Expected: 1
DROP TABLE e19_child;
DROP TABLE e19_parent;

-- ── E20. FOREIGN KEY — DELETE parent that has dependent children ──────────────
CREATE TABLE e20_dept (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE e20_emp  (
    id      INTEGER PRIMARY KEY,
    dept_id INTEGER REFERENCES e20_dept(id)
);
INSERT INTO e20_dept VALUES (1, 'Engineering');
INSERT INTO e20_emp  VALUES (1, 1);
INSERT INTO e20_emp  VALUES (2, 1);
-- Error: FK constraint failed — row in e20_dept is referenced by e20_emp
DELETE FROM e20_dept WHERE id = 1;
SELECT 'E20 fk delete parent with child' AS test, COUNT(*) AS cnt FROM e20_dept;
-- Expected: 1 (delete rejected)
DROP TABLE e20_emp;
DROP TABLE e20_dept;

-- ── E21. FOREIGN KEY — UPDATE parent PK that has dependent children ───────────
CREATE TABLE e21_dept (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE e21_emp  (
    id      INTEGER PRIMARY KEY,
    dept_id INTEGER REFERENCES e21_dept(id)
);
INSERT INTO e21_dept VALUES (1, 'Engineering');
INSERT INTO e21_emp  VALUES (1, 1);
-- Error: FK constraint failed — cannot modify e21_dept, row referenced by e21_emp
UPDATE e21_dept SET id = 99 WHERE id = 1;
SELECT 'E21 fk update parent pk' AS test, id FROM e21_dept;
-- Expected: 1 (update rejected)
DROP TABLE e21_emp;
DROP TABLE e21_dept;

-- ── E22. FOREIGN KEY — UPDATE child to reference non-existent parent ──────────
CREATE TABLE e22_dept (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE e22_emp  (
    id      INTEGER PRIMARY KEY,
    dept_id INTEGER REFERENCES e22_dept(id)
);
INSERT INTO e22_dept VALUES (1, 'Engineering');
INSERT INTO e22_emp  VALUES (1, 1);
-- Error: FK constraint failed — dept_id=99 doesn't exist in e22_dept
UPDATE e22_emp SET dept_id = 99 WHERE id = 1;
SELECT 'E22 fk update child bad parent' AS test, dept_id FROM e22_emp WHERE id = 1;
-- Expected: 1 (update rejected)
DROP TABLE e22_emp;
DROP TABLE e22_dept;

-- ── E23. STORED GENERATED COLUMN — INSERT specifies generated column ──────────
CREATE TABLE e23 (
    id         INTEGER PRIMARY KEY,
    qty        INTEGER NOT NULL,
    unit_price REAL    NOT NULL,
    total      REAL    AS (qty * unit_price) STORED
);
INSERT INTO e23 (id, qty, unit_price) VALUES (1, 3, 9.99);
-- Error: cannot assign to generated column 'total'
INSERT INTO e23 (id, qty, unit_price, total) VALUES (2, 1, 5.0, 999.0);
SELECT 'E23 generated col write' AS test, COUNT(*) AS cnt FROM e23;
-- Expected: 1 (only the valid row)
DROP TABLE e23;


-- ════════════════════════════════════════════════════════════════════════
-- DML ERRORS
-- ════════════════════════════════════════════════════════════════════════

-- ── E24. INSERT — column/value count mismatch ─────────────────────────────────
CREATE TABLE e24 (a INTEGER, b INTEGER);
INSERT INTO e24 VALUES (1, 2);
-- Error: 2 columns but 3 values provided
INSERT INTO e24 VALUES (1, 2, 3);
SELECT 'E24 col value mismatch' AS test, COUNT(*) AS cnt FROM e24;
-- Expected: 1
DROP TABLE e24;

-- ── E25. INSERT INTO VIEW — no INSTEAD OF trigger ────────────────────────────
CREATE TABLE e25_base (id INTEGER, name TEXT);
CREATE VIEW  e25_view AS SELECT * FROM e25_base;
-- Error: cannot insert into view 'e25_view' without an INSTEAD OF trigger
INSERT INTO e25_view VALUES (1, 'Alice');
SELECT 'E25 insert into view no trigger' AS test, COUNT(*) AS cnt FROM e25_base;
-- Expected: 0
DROP VIEW  e25_view;
DROP TABLE e25_base;

-- ── E26. SELECT — table does not exist ───────────────────────────────────────
-- Error: no such table: no_such_table
SELECT * FROM no_such_table;
SELECT 'E26 select missing table' AS test, 1 AS ok;


-- ════════════════════════════════════════════════════════════════════════
-- TRANSACTION ERRORS
-- ════════════════════════════════════════════════════════════════════════

-- ── E27. COMMIT — no active transaction ──────────────────────────────────────
-- Error: no active transaction
COMMIT;
SELECT 'E27 commit no tx' AS test, 1 AS ok;

-- ── E28. ROLLBACK — no active transaction ────────────────────────────────────
-- Error: no active transaction
ROLLBACK;
SELECT 'E28 rollback no tx' AS test, 1 AS ok;

-- ── E29. ROLLBACK TO SAVEPOINT — savepoint does not exist ────────────────────
BEGIN;
-- Error: no such savepoint 'no_sp'
ROLLBACK TO SAVEPOINT no_sp;
ROLLBACK;
SELECT 'E29 rollback to missing sp' AS test, 1 AS ok;

-- ── E30. RELEASE SAVEPOINT — savepoint does not exist ────────────────────────
BEGIN;
-- Error: no such savepoint 'no_sp'
RELEASE SAVEPOINT no_sp;
ROLLBACK;
SELECT 'E30 release missing sp' AS test, 1 AS ok;

-- ── E31. RELEASE SAVEPOINT — already released ────────────────────────────────
BEGIN;
SAVEPOINT sp_once;
RELEASE SAVEPOINT sp_once;
-- Error: savepoint 'sp_once' was already released
RELEASE SAVEPOINT sp_once;
ROLLBACK;
SELECT 'E31 release already-released sp' AS test, 1 AS ok;


-- ════════════════════════════════════════════════════════════════════════
-- PARSE / SYNTAX ERRORS
-- ════════════════════════════════════════════════════════════════════════

-- ── E32. Syntax error — incomplete SELECT ─────────────────────────────────────
-- Error: parse error
SELECT FROM;
SELECT 'E32 syntax error' AS test, 1 AS ok;

-- ── E33. Syntax error — unmatched opening parenthesis in subquery ────────────
-- Error: Unmatched ( in subquery
SELECT * FROM (;
SELECT 'E33 unmatched paren' AS test, 1 AS ok;

-- ── E34. Syntax error — incomplete CREATE TABLE ───────────────────────────────
-- Error: Expected table name after CREATE TABLE
CREATE TABLE;
SELECT 'E34 incomplete create table' AS test, 1 AS ok;
