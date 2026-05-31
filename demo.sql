-- Create a users table
CREATE TABLE IF NOT EXISTS users (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT    NOT NULL,
    email TEXT    UNIQUE,
    age   INTEGER
);

-- Seed some data
INSERT INTO users (name, email, age) VALUES ('Alice',   'alice@example.com',  30);
INSERT INTO users (name, email, age) VALUES ('Bob',     'bob@example.com',    25);
INSERT INTO users (name, email, age) VALUES ('Carol',   'carol@example.com',  35);
INSERT INTO users (name, email, age) VALUES ('Dave',    'dave@example.com',   28);

-- Query all users
SELECT * FROM users;

-- Query with a filter
SELECT name, age FROM users WHERE age > 27 ORDER BY age DESC;

-- Aggregate
SELECT COUNT(*) AS total_users, AVG(age) AS avg_age FROM users;
