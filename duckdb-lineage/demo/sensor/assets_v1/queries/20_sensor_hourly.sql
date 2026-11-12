-- 小时汇总：每台设备每小时的样本数、平均温度、最低电量
SELECT
  sensor_id,
  date_trunc('hour', measured_at) AS hour_ts,
  COUNT(*) AS samples,
  AVG(temperature) AS avg_temperature,
  MIN(battery) AS min_battery
FROM sensor_clean
GROUP BY sensor_id, date_trunc('hour', measured_at)
