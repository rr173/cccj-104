-- 场景 09：仅影响筛选、不参与值计算的列
-- customers.city 只出现在 WHERE，因此它只在 customer_name 的
-- control_sources 中；customer_name 的 value_sources 只含 customers.name。
-- 这是区分两类依赖的关键证据用例。
SELECT
    c.customer_id,
    c.name AS customer_name
FROM customers AS c
WHERE c.city = 'Shanghai'
  AND c.tier <> 'BANNED';
