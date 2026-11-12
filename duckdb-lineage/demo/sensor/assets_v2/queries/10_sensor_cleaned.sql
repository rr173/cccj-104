-- 清洗层（v2 更名：sensor_clean -> sensor_cleaned，口径不变）
SELECT
  sensor_id,
  measured_at,
  temperature,
  battery
FROM main.sensor_readings
WHERE status = 'OK'
  AND temperature BETWEEN -50.0 AND 80.0
