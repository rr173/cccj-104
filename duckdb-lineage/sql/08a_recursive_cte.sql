-- 场景 08a：递归 CTE -> unsupported
-- 递归固定点无法静态展开为列级血缘，必须明确拒绝。
WITH RECURSIVE walk(order_id, parent_id, depth) AS (
    SELECT order_id, order_id, 0
    FROM orders
    UNION ALL
    SELECT o.order_id, w.parent_id, w.depth + 1
    FROM orders AS o
    JOIN walk AS w ON w.order_id = o.customer_id
)
SELECT order_id, depth
FROM walk;
