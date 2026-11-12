-- 错误复制：把 s1 在 10:05 那行额外复制了一份（重复行数 +1）
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE status = 'OK'
UNION ALL
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE sensor_id = 's1'
  AND measured_at = TIMESTAMP '2026-03-01 10:05:00'
