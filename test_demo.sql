CREATE TABLE IF NOT EXISTS products (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT    NOT NULL,
    price    REAL    NOT NULL,
    stock    INTEGER DEFAULT 0
);

INSERT INTO products (name, price, stock) VALUES ('Laptop',  999.99, 10);
INSERT INTO products (name, price, stock) VALUES ('Mouse',    29.99, 50);
INSERT INTO products (name, price, stock) VALUES ('Monitor', 399.99,  5);
INSERT INTO products (name, price, stock) VALUES ('Keyboard', 79.99, 20);

SELECT * FROM products;

SELECT name, price FROM products WHERE price < 100 ORDER BY price ASC;

SELECT COUNT(*) AS total_products, SUM(stock) AS total_stock, AVG(price) AS avg_price FROM products;

UPDATE products SET stock = stock - 1 WHERE name = 'Laptop';

SELECT name, stock FROM products WHERE name = 'Laptop';

DELETE FROM products WHERE stock > 15;

SELECT * FROM products;
