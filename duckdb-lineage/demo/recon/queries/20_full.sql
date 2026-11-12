-- 基线：全部有效读数
SELECT sensor_id, measured_at, temperature
FROM sensor_readings
WHERE status = 'OK'
