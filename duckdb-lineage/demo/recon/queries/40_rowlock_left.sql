-- 整行配对反例：左 (a,b) = (1,1),(2,2)
SELECT k, a, b
FROM measurements
WHERE grp = 'rowlock'
ORDER BY a
