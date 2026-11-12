-- 同值集合，打乱返回顺序；NaN 只等于 NaN，±Inf 分符号，NULL 只等于 NULL
SELECT k, f
FROM edge_numbers
ORDER BY f NULLS LAST
