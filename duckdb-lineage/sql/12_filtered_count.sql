-- 场景 12：筛选后的派生关系被外层 COUNT(*) 消费
-- 改 orders.status 会改变计数条数；即使外层不引用内层任何列，
-- status 也必须出现在计数字段 n 的 control_sources 中，
-- 行集标记 row_set:shop.main.orders 仍只承担 value 角色。
WITH paid_orders AS (
    SELECT order_id
    FROM orders
    WHERE status = 'PAID'
)
SELECT COUNT(*) AS paid_order_count
FROM paid_orders;
