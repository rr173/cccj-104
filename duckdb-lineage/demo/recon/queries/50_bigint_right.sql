-- 改写侧只改投影顺序无关的写法，值集合必须完全相同；若改坏一个大整数，
-- 工具必须能发现（这里给出保持不变的正确版本）
SELECT id, huge_val, dec_val
FROM exact_numbers
ORDER BY id DESC
