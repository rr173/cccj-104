-- 列结构不符（新侧）：列名/类型与旧侧不同，应在比较前被拒绝
SELECT id, label AS name
FROM empty_source
