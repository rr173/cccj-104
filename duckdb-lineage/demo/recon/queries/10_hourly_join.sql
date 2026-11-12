-- 新写法：JOIN 预聚合结果替代相关嵌套子查询（口径不变）
WITH base AS (
  SELECT sensor_id,
         date_trunc('hour', measured_at) AS hour_utc,
         temperature
  FROM sensor_readings
  WHERE status = 'OK'
)
SELECT b.sensor_id,
       b.hour_utc,
       agg.avg_temperature
FROM base b
JOIN (
  SELECT sensor_id,
         date_trunc('hour', measured_at) AS hour_utc,
         avg(temperature) AS avg_temperature
  FROM sensor_readings
  WHERE status = 'OK'
  GROUP BY sensor_id, date_trunc('hour', measured_at)
) agg
  ON agg.sensor_id = b.sensor_id AND agg.hour_utc = b.hour_utc
GROUP BY b.sensor_id, b.hour_utc, agg.avg_temperature
