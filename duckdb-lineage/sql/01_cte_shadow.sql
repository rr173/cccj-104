-- 场景 01：CTE 遮蔽同名基础表
-- CTE 名与基础表 orders 同名；CTE 内部的 o 仍指向基础表，
-- 外层查询中的 o 必须解析到 CTE，而不是基础表 orders。
WITH orders AS (
    SELECT
        o.customer_id,
        o.amount * 1.0 AS amount
    FROM shop.main.orders AS o
    WHERE o.status = 'PAID'
)
SELECT
    c.customer_id,
    c.name,
    o.amount
FROM customers AS c
JOIN orders AS o
    ON o.customer_id = c.customer_id;
