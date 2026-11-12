-- 场景 14：内部只选常量的相关 EXISTS（行存在性判断）
-- 子查询不投影任何基础列，仅凭 orders 是否存在匹配行决定客户记录是否保留。
-- 删空 orders 后外层结果随之消失，因此：
--   row_set:shop.main.orders 必须出现在 customer_name 的 control_sources；
--   orders.status / 两侧 customer_id 同为 control；
--   任何 orders 列都不得进入 value_sources。
SELECT
    c.customer_id,
    c.name AS customer_name
FROM customers AS c
WHERE EXISTS (
    SELECT 1
    FROM orders AS o
    WHERE o.customer_id = c.customer_id
      AND o.status = 'PAID'
);
