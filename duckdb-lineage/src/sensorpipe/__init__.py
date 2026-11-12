"""sensorpipe：传感器历史读数的本地补算处理链。

职责边界（与 lineage / viewctl 相互独立）：

* 读数与校准记录落在一个 DuckDB 库文件里（``sensor_readings`` /
  ``sensor_calibrations``）；
* 每条读数有唯一 ``reading_id``、原始温度与一条可查询的明细结论
  （``sensor_reading_results``）：命中的校准、修正温度、是否纳入指标、原因；
* 小时指标（``sensor_hourly``）只由"status=OK 且命中校准且修正温度在
  [-50, 80] 内"的读数构成，按设备与 UTC 小时汇总；
* 校准变更（新增/修改/撤销）在同一个事务里重算受影响读数与小时指标，
  并记录影响范围（``sensor_pipeline_runs`` / ``sensor_run_details``）；
* 所有时刻一律按 UTC 存储与匹配，不依赖进程或库连接的本地时区。
"""

from .core import (
    apply_calibrations,
    import_readings,
    query_detail,
    query_hourly,
    query_runs,
    rebuild_all,
    status,
)
from .db import connect, init_db, initialize
from .errors import DomainError

__all__ = [
    "apply_calibrations",
    "import_readings",
    "query_detail",
    "query_hourly",
    "query_runs",
    "rebuild_all",
    "status",
    "connect",
    "initialize",
    "init_db",
    "DomainError",
]
