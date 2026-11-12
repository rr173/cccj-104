-- 场景 07：未限定列在两个绑定中同名 -> 歧义诊断
-- customer_id 同时存在于 customers 与 orders，未限定的 SELECT customer_id
-- 必须报 ambiguous_column，并给出两个候选与行列定位，不能静默选一个。
SELECT
    customer_id,
    c.name,
    o.order_id
FROM customers AS c
JOIN orders AS o
    ON o.customer_id = c.customer_id;
