-- 场景 13：连接后只取对侧姓名 + 嵌套标量子查询的角色分桶
-- 1) 派生表 paid_orders 内部的 status 筛选决定客户行能否进入结果，
--    即使外层只取 customers.name，status 仍是 customer_name 的 control；
-- 2) 标量子查询按付款状态取最大金额：amount 只能是 value，
--    status/customer_id 只能是 control，角色不得互换。
SELECT
    c.name AS customer_name,
    (
        SELECT MAX(o.amount)
        FROM orders AS o
        WHERE o.customer_id = c.customer_id
          AND o.status = 'PAID'
    ) AS max_paid_amount
FROM customers AS c
JOIN (
    SELECT DISTINCT customer_id
    FROM orders
    WHERE status = 'PAID'
) AS p ON p.customer_id = c.customer_id;
