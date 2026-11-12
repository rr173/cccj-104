-- 在线设备数（v2 起撤下，由 hourly 直接覆盖该诉求）
SELECT COUNT(DISTINCT sensor_id) AS active_sensors
FROM sensor_clean
