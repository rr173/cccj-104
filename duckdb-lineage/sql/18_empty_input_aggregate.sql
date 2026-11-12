-- 场景 18：空输入仍返回一行的无分组 COUNT，行筛选不得扩散为外层行选择
-- 内层 `SELECT COUNT(*) ... WHERE status='PAID'` 恒为一行：删空 orders 后
-- 返回 0 而不是无行。因此：
--   n 的取值仍依赖订单行（行集标记在 value 桶）+ status（改变计数取值，
--   属 control，匹配过滤）；
--   外层固定标记 marker 不得因内层 status 而产生任何依赖：内层 WHERE 只
--   改变聚合输入，不是派生关系的"行选择"来源，不能扩散到外层每个输出。
SELECT
    t.paid_count AS n,
    'fixed' AS marker
FROM (
    SELECT COUNT(*) AS paid_count
    FROM orders
    WHERE status = 'PAID'
) AS t;
