-- 场景 08c：语法解析失败 -> parse_error，带行列定位
-- WHERE 条件在等号处截断，sqlglot 无法解析。
SELECT order_id, amount
FROM orders
WHERE status = ;
