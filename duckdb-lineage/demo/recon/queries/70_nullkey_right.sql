-- 同数据换顺序；NULL 键两条重复行必须仍按多重集合配成两对
SELECT k, v
FROM tiny_groups
ORDER BY v DESC
