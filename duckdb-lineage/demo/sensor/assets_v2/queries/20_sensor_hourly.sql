-- 小时汇总（v2：上游同步更名为 sensor_cleaned）
SELECT
  sensor_id,
  date_trunc('hour', measured_at) AS hour_ts,
  COUNT(*) AS samples,
  AVG(temperature) AS avg_temperature,
  MIN(battery) AS min_battery
FROM sensor_cleaned
GROUP BY sensor_id, date_trunc('hour', measured_at)
