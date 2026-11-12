-- 空 vs 非空：左边 0 行，右边凭空多出一行（差异，不能把空结果当通过）
SELECT id, label
FROM empty_source
UNION ALL
SELECT -1 AS id, 'phantom' AS label
