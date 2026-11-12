-- 集合查询写法 B：分支顺序相反（返回顺序被忽略，多重集合应一致）
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE status = 'FAULT'
UNION ALL
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE status = 'OK'
