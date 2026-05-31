-- Orders table linking users to products
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity   INTEGER DEFAULT 1,
    FOREIGN KEY (user_id)    REFERENCES users(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

INSERT INTO orders (user_id, product_id, quantity) VALUES (1, 1, 1);
INSERT INTO orders (user_id, product_id, quantity) VALUES (1, 3, 2);
INSERT INTO orders (user_id, product_id, quantity) VALUES (2, 3, 1);
INSERT INTO orders (user_id, product_id, quantity) VALUES (3, 1, 1);
INSERT INTO orders (user_id, product_id, quantity) VALUES (4, 3, 3);

-- INNER JOIN: who ordered what
SELECT u.name AS customer, p.name AS product, o.quantity
FROM orders o
INNER JOIN users    u ON o.user_id    = u.id
INNER JOIN products p ON o.product_id = p.id
ORDER BY u.name;

-- LEFT JOIN: all users, even those with no orders
SELECT u.name AS customer, p.name AS product
FROM users u
LEFT JOIN orders  o ON o.user_id    = u.id
LEFT JOIN products p ON o.product_id = p.id
ORDER BY u.name;

-- Aggregate: total spent per user
SELECT u.name AS customer,
       COUNT(o.id)                        AS total_orders,
       SUM(p.price * o.quantity)          AS total_spent
FROM users u
INNER JOIN orders   o ON o.user_id    = u.id
INNER JOIN products p ON o.product_id = p.id
GROUP BY u.id, u.name
ORDER BY total_spent DESC;

-- Subquery: users who spent more than average
SELECT name, total_spent FROM (
    SELECT u.name AS name,
           SUM(p.price * o.quantity) AS total_spent
    FROM users u
    INNER JOIN orders   o ON o.user_id    = u.id
    INNER JOIN products p ON o.product_id = p.id
    GROUP BY u.id, u.name
) AS summary
WHERE total_spent > (SELECT AVG(p2.price) FROM products p2);

-- CTE: most ordered products
WITH order_counts AS (
    SELECT product_id, SUM(quantity) AS total_sold
    FROM orders
    GROUP BY product_id
)
SELECT p.name AS product, oc.total_sold
FROM order_counts oc
INNER JOIN products p ON p.id = oc.product_id
ORDER BY oc.total_sold DESC;
