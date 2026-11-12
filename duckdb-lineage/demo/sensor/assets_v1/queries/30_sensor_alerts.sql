-- 告警层：平均温度达到 30 度报高温，否则电量低于 60 报低电量
SELECT
  sensor_id,
  hour_ts,
  avg_temperature,
  min_battery,
  CASE
    WHEN avg_temperature >= 30.0 THEN 'HIGH_TEMP'
    ELSE 'LOW_BATTERY'
  END AS alert_kind
FROM sensor_hourly
WHERE avg_temperature >= 30.0
   OR min_battery < 60.0
