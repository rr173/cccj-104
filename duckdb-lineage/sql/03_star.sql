-- 场景 03：星号展开（裸 * 与限定 t.*）
-- 裸 * 展开 FROM/JOIN 全部关系的列；c.* 只展开 customers 的列。
SELECT
    *,
    c.*
FROM customers AS c
JOIN orders AS o
    ON o.customer_id = c.customer_id
WHERE o.order_date >= DATE '2026-01-01';
