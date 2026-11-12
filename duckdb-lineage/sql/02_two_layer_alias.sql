-- 场景 02：两层别名（基础表 -> 派生表 -> 外层）
-- 派生表 d 把 order_id 重命名为 oid；外层 d.oid 必须重定基到
-- 基础列 shop.main.order_items.order_id，不能生成 "d.oid" 这种拼接假边。
SELECT
    d.oid,
    d.net_amount,
    p.product_name
FROM (
    SELECT
        oi.order_id  AS oid,
        oi.product_id,
        oi.qty * oi.unit_price AS net_amount
    FROM order_items AS oi
    WHERE oi.qty > 0
) AS d
JOIN products AS p
    ON p.product_id = d.product_id;
