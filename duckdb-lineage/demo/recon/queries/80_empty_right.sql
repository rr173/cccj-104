-- 空结果（新）：同样 0 行，空多重集合应当相等；空结果不被当成通过的豁免
SELECT id, label
FROM empty_source
WHERE id > 0
