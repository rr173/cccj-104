"""viewctl 的错误载荷约定。

所有面向使用者的失败都表达为结构化错误载荷（不写库、不抛裸异常）：

    {"code": <机器可读码>, "message": <人可读说明>, ...附加字段}

错误码一览：

  manifest_error      清单 JSON 无法解析 / 结构不合法 / 视图名不合法
  duplicate_name      清单规范化后出现重名（大小写不敏感）
  missing_resource    清单声明的查询文件缺失或不可读
  query_error         查询文件不是单条 SELECT / 含 DDL/DML / 解析失败
  cycle               候选视图之间存在循环引用（含自引用）
  drop_blocked        撤下对象仍被保留视图或外部视图引用（附依赖链）
  name_collision      候选名字与库内非本工具管理的对象冲突
  object_type_conflict 已登记对象在库内变成了表等其它类型
  bind_error          试装/正式发布时 DuckDB 绑定失败（列不存在、类型不兼容等）
  ddl_error           试装/正式发布时 DDL 执行失败
  stale_source        预览后资产文件发生变化
  stale_database      预览后库内登记版本已被推进
  io_error            文件/数据库无法打开等 IO 问题
"""

from __future__ import annotations

from typing import Any


def err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return payload


class OpFailure(Exception):
    """试装/应用执行期单步失败（携带结构化载荷，由上层回滚后输出）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message", ""))
        self.payload = payload


class StaleDatabase(Exception):
    """事务内版本核对失败（可观察的库内版本与预览令牌不一致）。"""
