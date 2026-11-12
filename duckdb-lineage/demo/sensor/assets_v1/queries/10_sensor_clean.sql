-- 清洗层：剔除故障标记与超量程温度（有效量程 -50 ~ 80 摄氏度）
SELECT
  sensor_id,
  measured_at,
  temperature,
  battery
FROM main.sensor_readings
WHERE status = 'OK'
  AND temperature BETWEEN -50.0 AND 80.0
