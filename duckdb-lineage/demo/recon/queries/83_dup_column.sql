-- 重名输出列（新侧）：两个同名 label，无法按名字稳定寻址，必须拒绝
SELECT id, label, label
FROM empty_source
