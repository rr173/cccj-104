-- 右 (a,b) = (1,2),(2,1)：a 列、b 列各自都能配上，整行却没有一对相同
SELECT k, a, b
FROM measurements
WHERE grp = 'rowlock_r'
ORDER BY b
