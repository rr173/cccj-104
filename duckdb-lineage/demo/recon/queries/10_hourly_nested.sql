-- 旧写法：相关嵌套标量子查询算小时均温，DISTINCT 去重
WITH base AS (
  SELECT sensor_id,
         date_trunc('hour', measured_at) AS hour_utc,
         temperature
  FROM sensor_readings
  WHERE status = 'OK'
)
SELECT DISTINCT b.sensor_id,
       b.hour_utc,
       (SELECT avg(sub.temperature)
          FROM sensor_readings sub
         WHERE sub.sensor_id = b.sensor_id
           AND date_trunc('hour', sub.measured_at) = b.hour_utc
           AND sub.status = 'OK') AS avg_temperature
FROM base b
