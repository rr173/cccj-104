-- 贪心陷阱组 trap（同键 G 的两行），按某个顺序返回
SELECT k, a
FROM measurements
WHERE grp = 'trap'
ORDER BY a DESC
