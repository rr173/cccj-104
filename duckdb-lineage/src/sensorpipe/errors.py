"""sensorpipe 的结构化错误约定。

面向使用者的失败都返回 JSON（不抛裸异常）：

    {"status": "error", "errors": [{"code": ..., "message": ..., ...}]}

错误码：

  usage_error               命令行参数/输入选择不合法
  io_error                  文件、stdin 或数据库无法读取
  not_initialized           目标库尚未执行 init（缺表）
  input_error               JSONL 行解析失败或字段不合法（附 line/field）
  reading_conflict          同一 reading_id 的内容与库内（或本批次内）不同
  duplicate_calibration_id  同一批校准输入里重复出现 calibration_id
  unknown_calibration       撤销一个不存在的 calibration_id
  calibration_overlap       同设备的两条生效校准时段的交集非空
  db_error                  数据库事务执行失败（已回滚）
"""

from __future__ import annotations

from typing import Any


def err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return payload


class DomainError(Exception):
    """可预期的领域失败；CLI 将其转成结构化错误载荷。"""

    def __init__(self, errors: list[dict[str, Any]], *, exit_code: int = 2) -> None:
        super().__init__(errors[0]["message"] if errors else "sensorpipe error")
        self.errors = errors
        self.exit_code = exit_code

    @classmethod
    def one(cls, code: str, message: str, *, exit_code: int = 2, **extra: Any) -> "DomainError":
        return cls([err(code, message, **extra)], exit_code=exit_code)
