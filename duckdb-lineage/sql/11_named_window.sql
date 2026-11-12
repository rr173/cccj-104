-- 场景 11：命名窗口 WINDOW 子句 + 跨 schema 限定表名
-- 窗口定义在 WINDOW 子句中命名，PARTITION/ORDER 的 control 血缘必须能追溯；
-- 表名使用 catalog.schema.table 三段限定，验证目录解析与稳定身份。
SELECT
    s.staff_id,
    s.full_name,
    ROW_NUMBER() OVER w AS region_seq
FROM hr.main.staff AS s
WINDOW w AS (PARTITION BY s.region_id ORDER BY s.hire_date);
