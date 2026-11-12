-- 改写侧值集合 trap_r = [2.00, 2.10]，与左侧 [2.04, 1.95] 不同但可在容差内配对
SELECT k, a
FROM measurements
WHERE grp = 'trap_r'
ORDER BY a ASC
