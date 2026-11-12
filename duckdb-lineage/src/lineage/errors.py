"""血缘抽取的错误模型。

设计原则：任何无法 *保证正确* 的输入都抛出 :class:`LineageUnsupported`，
由上层转换为 ``status=unsupported`` 的结构化结果，绝不输出"完整血缘"的假结果。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class ErrorLocation:
    """SQL 文本中的错误定位（1 起始行列号，与 sqlglot/DuckDB 对齐）。"""

    line: int
    col: int
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LineageError(Exception):
    """所有血缘分析错误的基类。"""

    code = "lineage_error"

    def __init__(self, message: str, location: ErrorLocation | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.location = location

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "location": self.location.to_dict() if self.location else None,
        }


class LineageUnsupported(LineageError):
    """语法/特性在受支持方言范围之外（递归 CTE、动态 SQL、LATERAL 等）。"""

    code = "unsupported_syntax"


class LineageParseError(LineageError):
    """SQL 无法通过 sqlglot DuckDB 方言解析。"""

    code = "parse_error"


class AmbiguousColumnError(LineageError):
    """未限定列名在当前作用域存在多个候选绑定。"""

    code = "ambiguous_column"

    def __init__(
        self,
        column: str,
        candidates: list[str],
        location: ErrorLocation | None = None,
    ) -> None:
        super().__init__(
            f"列 '{column}' 存在歧义，候选绑定: {', '.join(candidates)}",
            location,
        )
        self.column = column
        self.candidates = candidates

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["column"] = self.column
        payload["candidates"] = self.candidates
        return payload


class UnknownColumnError(LineageError):
    """列（或表）在当前作用域及全部外层作用域中都无法绑定。"""

    code = "unknown_column"

    def __init__(self, name: str, location: ErrorLocation | None = None) -> None:
        super().__init__(f"无法解析列或别名: '{name}'", location)
        self.name = name


class UnknownRelationError(LineageError):
    """FROM/JOIN 中的关系不在 catalog 目录中，且不是可见的 CTE/派生表。"""

    code = "unknown_relation"

    def __init__(self, fq_name: str, location: ErrorLocation | None = None) -> None:
        super().__init__(f"目录中找不到关系: {fq_name}", location)
        self.fq_name = fq_name
