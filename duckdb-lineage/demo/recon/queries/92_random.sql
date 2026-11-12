-- 非确定性表达式：每次运行可能变化，不允许进入对账查询
SELECT sensor_id, random() AS r FROM sensor_readings
