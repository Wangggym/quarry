-- Seed for the local test database used by the suite.
--   createdb quarry_test   (or: CREATE DATABASE quarry_test)
--   psql quarry_test -f tests/seed.sql
DROP TABLE IF EXISTS orders, customers;
CREATE TABLE customers (
    id         serial PRIMARY KEY,
    name       text NOT NULL,
    email      text UNIQUE,
    created_at timestamptz DEFAULT now()
);
CREATE TABLE orders (
    id          serial PRIMARY KEY,
    customer_id int REFERENCES customers(id),
    amount      numeric(10,2),
    status      text,
    created_at  timestamptz DEFAULT now()
);
INSERT INTO customers (name, email) VALUES
    ('Alice', 'alice@ex.com'), ('Bob', 'bob@ex.com'), ('Carol', 'carol@ex.com');
INSERT INTO orders (customer_id, amount, status) VALUES
    (1, 99.50, 'paid'), (1, 12.00, 'paid'), (2, 250.00, 'pending'), (3, 5.25, 'refunded');
