-- 场景 04：相关 EXISTS（外层绑定穿透子查询）
-- 子查询 WHERE oi.order_id = o.order_id 中的 o 是外层 orders 的相关引用；
-- EXISTS 内部全部来源只进 control_sources，不产生值依赖。
SELECT
    o.order_id,
    o.customer_id,
    o.amount
FROM orders AS o
WHERE o.status = 'PAID'
  AND EXISTS (
      SELECT 1
      FROM order_items AS oi
      WHERE oi.order_id = o.order_id
        AND oi.qty >= 2
  );
