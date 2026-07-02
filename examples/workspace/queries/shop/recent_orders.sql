-- @name: recent_orders
-- @db: shop
-- @desc: Latest orders with customer names
-- @param: days (int, default=7)
SELECT o.id, c.name, o.amount, o.status, o.created_at
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.created_at > now() - make_interval(days => :days)
ORDER BY o.created_at DESC;
