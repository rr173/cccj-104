-- 场景 10：HAVING 引用派生列，过滤条件需继续递归到基础身份
-- HAVING 引用投影别名 total_amount；该别名派生自 SUM(oi.qty * oi.unit_price)，
-- 因此 total_amount 字段同时带 value 与 control 两类指向同一批基础列的边。
SELECT
    oi.order_id,
    SUM(oi.qty * oi.unit_price) AS total_amount
FROM order_items AS oi
GROUP BY oi.order_id
HAVING SUM(oi.qty * oi.unit_price) > 1000;
