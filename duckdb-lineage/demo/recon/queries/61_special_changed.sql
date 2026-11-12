-- 边界值差异版本：额外引入一个 NaN，NaN 数量多 1（应判差异）
SELECT k, f FROM edge_numbers
UNION ALL
SELECT 'e' AS k, CAST('nan' AS DOUBLE) AS f
