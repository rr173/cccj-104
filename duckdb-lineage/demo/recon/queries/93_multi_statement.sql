-- 多语句：第二条是写操作，必须整体拒绝
SELECT count(*) FROM sensor_readings;
DELETE FROM sensor_readings WHERE status = 'FAULT'
