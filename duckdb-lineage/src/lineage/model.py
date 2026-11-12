"""血缘图的数据模型与确定性序列化。

两类来源严格区分：

* ``value_sources``：参与投影表达式 *值计算* 的来源；
* ``control_sources``：只影响结果 *存在性/选择/分组/排序* 的来源
  （WHERE / HAVING / JOIN ON / QUALIFY、CASE 条件、窗口 PARTITION BY/ORDER BY）。

每个来源都带 ``path``，即它经过的表达式位置序列，便于变更审查时还原上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 基础列身份形如 catalog.schema.table.column；
# 行集标记形如 row_set:catalog.schema.table，用于 COUNT(*) 这类无参数聚合。
ROW_SET_PREFIX = "row_set:"


def row_set_marker(fq_table: str) -> str:
    return f"{ROW_SET_PREFIX}{fq_table}"


def is_row_set_marker(ref: str) -> bool:
    return ref.startswith(ROW_SET_PREFIX)


@dataclass
class Source:
    """单个血缘来源。"""

    ref: str                       # 基础列身份或行集标记
    kind: str                      # "base_column" | "row_set"
    path: tuple[str, ...]          # 经过的表达式位置

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "kind": self.kind, "path": list(self.path)}


@dataclass
class ExprSources:
    """表达式分析的两个来源桶；在作用域各层之间传递时保持分桶不串味。"""

    value: list[Source] = field(default_factory=list)
    control: list[Source] = field(default_factory=list)

    def extend(self, other: "ExprSources") -> None:
        self.value.extend(other.value)
        self.control.extend(other.control)


@dataclass
class FieldLineage:
    """一个输出字段的完整血缘。"""

    name: str
    relation: str                  # 输出关系（顶层查询/CTE 名）
    value_sources: list[Source] = field(default_factory=list)
    control_sources: list[Source] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relation": self.relation,
            "value_sources": _dedupe_sources(self.value_sources),
            "control_sources": _dedupe_sources(self.control_sources),
        }


@dataclass
class LineageResult:
    """一次分析的完整结果（成功或 unsupported 二选一，不允许"部分血缘伪装完整"）。"""

    status: str                    # "ok" | "unsupported"
    dialect: str
    sql_file: str | None
    output_relation: str | None
    fields: list[FieldLineage] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dialect": self.dialect,
            "sql_file": self.sql_file,
            "output_relation": self.output_relation,
            "fields": [f.to_dict() for f in self.fields],
            "errors": self.errors,
        }


def _dedupe_sources(sources: list[Source]) -> list[dict[str, Any]]:
    """同一 (ref, kind) 合并路径，并做确定性排序。"""
    merged: dict[tuple[str, str], set[tuple[str, ...]]] = {}
    for src in sources:
        merged.setdefault((src.ref, src.kind), set()).add(src.path)
    out: list[dict[str, Any]] = []
    for (ref, kind), paths in merged.items():
        out.append(
            {
                "ref": ref,
                "kind": kind,
                "paths": [list(p) for p in sorted(paths)],
            }
        )
    out.sort(key=lambda d: (d["ref"], d["kind"]))
    return out
