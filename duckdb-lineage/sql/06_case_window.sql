-- 场景 06：CASE 与窗口表达式
-- CASE 条件列(tier)进 control，分支列进 value；
-- 窗口 PARTITION BY / ORDER BY 进 control，窗口参数进 value；
-- COUNT(*) 记行集标记 row_set:*，不伪造全列依赖。
SELECT
    c.customer_id,
    o.order_id,
    CASE
        WHEN c.tier = 'VIP' THEN c.name
        ELSE 'anonymous'
    END AS customer_label,
    SUM(o.amount) OVER (
        PARTITION BY c.city
        ORDER BY o.order_date
    ) AS running_amount,
    ROW_NUMBER() OVER (
        PARTITION BY c.city
        ORDER BY o.amount DESC
    ) AS city_rank,
    COUNT(*) OVER (
        PARTITION BY c.city
    ) AS city_row_counter
FROM customers AS c
JOIN orders AS o
    ON o.customer_id = c.customer_id
WHERE o.status IN ('PAID', 'SHIPPED');
