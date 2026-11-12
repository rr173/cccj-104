-- 告警层（v2：高温阈值 30 -> 31.5）
SELECT
  sensor_id,
  hour_ts,
  avg_temperature,
  min_battery,
  CASE
    WHEN avg_temperature >= 31.5 THEN 'HIGH_TEMP'
    ELSE 'LOW_BATTERY'
  END AS alert_kind
FROM sensor_hourly
WHERE avg_temperature >= 31.5
   OR min_battery < 60.0
