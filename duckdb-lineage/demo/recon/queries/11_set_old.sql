-- 集合查询写法 A：两个分支 UNION ALL
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE status = 'OK'
UNION ALL
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE status = 'FAULT'
