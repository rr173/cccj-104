-- 场景 05：UNION ALL 按位置合并，两个分支列名不同
-- 输出字段名以左分支为准（id / amt），但血缘同时包含右分支的基础身份。
SELECT
    o.order_id AS id,
    o.amount   AS amt
FROM orders AS o
WHERE o.status = 'PAID'

UNION ALL

SELECT
    oi.item_id,
    oi.qty * oi.unit_price
FROM order_items AS oi
WHERE oi.qty > 0;
