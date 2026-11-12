-- 场景 16：NOT EXISTS 的存在性语义与 EXISTS 对称
-- 删空 orders 后条件由"没有匹配订单"恒真变为恒假：结果翻转来自行集为空，
-- 因此 orders 行集标记必须可追溯；内层匹配列 status 仍属 control。
SELECT
    c.customer_id,
    c.name AS customer_name
FROM customers AS c
WHERE NOT EXISTS (
    SELECT 1
    FROM orders AS o
    WHERE o.customer_id = c.customer_id
      AND o.status = 'PAID'
);
