-- 场景 17：带匹配谓词的标量常量 —— 删掉最后一条匹配记录后结果从 1 变成 NULL
-- 子查询只返回常量 1，但它是"有没有匹配行"的行集判断：没有匹配行时标量结果
-- 为 NULL，而不是 0/1。因此：
--   匹配条件 o.customer_id / o.status -> control；
--   orders 行集标记 -> existence（NULL 变化必须追到行集）；
--   子查询里没有任何基础列进入 value（常量不是基础列）。
SELECT
    c.customer_id,
    c.name AS customer_name,
    (
        SELECT 1
        FROM orders AS o
        WHERE o.customer_id = c.customer_id
          AND o.status = 'PAID'
    ) AS has_paid_order
FROM customers AS c;
