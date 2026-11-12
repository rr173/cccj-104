-- 写操作藏在 WITH 的 CTE 体内：不能只看开头是不是 SELECT
WITH changed AS (
  UPDATE sensor_readings SET status = 'OK' WHERE status = 'FAULT'
  RETURNING sensor_id
)
SELECT sensor_id FROM changed
