-- 场景 15：EXISTS 的 SELECT 列表只是占位，不是筛选条件
-- 子查询投影里的 amount/status 不参与谓词判断：无论把这两列改成什么值，
-- 客户行是否被保留只取决于 WHERE 的 customer_id 匹配与 orders 是否有行。
-- 因此 amount/status 不得出现在任何来源桶；匹配列进 control，行集进 existence。
SELECT
    c.customer_id,
    c.name AS customer_name
FROM customers AS c
WHERE EXISTS (
    SELECT o.amount, o.status
    FROM orders AS o
    WHERE o.customer_id = c.customer_id
);
