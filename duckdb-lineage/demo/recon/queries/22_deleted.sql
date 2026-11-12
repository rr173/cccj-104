-- 错误删除：改写后漏掉了 s1 在 11:05 的读数（少一行）
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE status = 'OK'
  AND NOT (sensor_id = 's1' AND measured_at = TIMESTAMP '2026-03-01 11:05:00')
