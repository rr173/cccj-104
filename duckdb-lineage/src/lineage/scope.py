"""作用域与绑定。

每个 SELECT（含子查询里的 SELECT）拥有一个 :class:`Scope`。作用域链通过
``parent`` 连接，名字解析沿链向上查找，从而自然支持相关子查询引用外层绑定。

绑定分两种：

* :class:`TableBinding`：目录中的基础表，列解析为 ``catalog.schema.table.column``；
* :class:`DerivedBinding`：子查询/CTE 等派生关系，列解析结果需要 *重定基*
  （rebase）到派生关系内部字段的基础身份，而不是把两层同名别名拼成假边。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from .catalog import Catalog, TableSchema
from .errors import AmbiguousColumnError, ErrorLocation, UnknownColumnError
from .model import ExprSources, Source


@dataclass
class TableBinding:
    alias: str                     # FROM 中的别名（无别名时为表名）
    table: TableSchema

    @property
    def names(self) -> list[str]:
        return list(self.table.columns)

    def resolve_column(
        self, column: str, path: tuple[str, ...]
    ) -> ExprSources:
        if column not in self.table.columns:
            return ExprSources()
        return ExprSources(
            value=[
                Source(
                    ref=self.table.column_identity(column),
                    kind="base_column",
                    path=path,
                )
            ]
        )


@dataclass
class DerivedBinding:
    """派生关系绑定：CTE、FROM (SELECT ...) 子查询。

    ``inner_fields`` 记录派生关系每个输出列名 -> 该列内部已算出的来源集合。
    解析外层引用时，把内层来源的路径前置外层访问路径，完成重定基。
    """

    alias: str
    column_names: list[str]
    # name -> {"value": list[Source], "control": list[Source]}
    inner_fields: dict[str, dict[str, list[Source]]]
    # 派生关系内部可见的行集标记（供 COUNT(*) 跨层透传）
    row_sets: list[Source] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return list(self.column_names)

    def resolve_column(
        self,
        column: str,
        path: tuple[str, ...],
    ) -> ExprSources:
        """重定基时严格保留内层的 value/control 分桶。"""
        if column not in self.inner_fields:
            return ExprSources()
        inner = self.inner_fields[column]
        return ExprSources(
            value=[
                Source(ref=s.ref, kind=s.kind, path=path + s.path)
                for s in inner["value"]
            ],
            control=[
                Source(ref=s.ref, kind=s.kind, path=path + s.path)
                for s in inner["control"]
            ],
        )


Binding = TableBinding | DerivedBinding


@dataclass
class Scope:
    """一个 SELECT 块的词法作用域。"""

    node: exp.Select
    parent: "Scope | None"
    catalog: Catalog
    # 节点定位器（可选）；存在时错误带行列号
    positions: object | None = None
    # 本作用域 FROM/JOIN 引入的关系绑定，别名 -> 绑定
    bindings: dict[str, Binding] = field(default_factory=dict)
    # 本作用域可见的 CTE：CTE 名 -> 已分析的派生绑定
    ctes: dict[str, DerivedBinding] = field(default_factory=dict)
    # 本作用域投影别名 -> 内部来源（供 HAVING/QUALIFY/ORDER BY 引用派生列）
    projection_aliases: dict[str, dict[str, list[Source]]] = field(default_factory=dict)
    # 输出关系名（顶层查询的别名或 CTE 名）
    relation_name: str = "<query>"
    # 子查询 SELECT 节点 id -> 子作用域（EXISTS / 标量子查询 / IN 子查询回查）
    child_scopes: dict[int, "Scope"] = field(default_factory=dict)
    # 命名窗口 WINDOW w AS (...)：窗口名 -> Window 定义节点
    named_windows: dict[str, exp.Window] = field(default_factory=dict)
    # 本 SELECT 的输出字段（分析完成后填充），供外层派生绑定/子查询重定基
    output_fields: list[dict[str, Any]] = field(default_factory=list)
    # 本 SELECT 关系层面的全部来源（WHERE/JOIN ON/HAVING/QUALIFY + 投影），
    # 供 EXISTS 整体作为 control 上报
    relation_sources: list[Source] = field(default_factory=list)
    # 本层可见的行集标记（COUNT(*) 透传用）
    row_sets: list[Source] = field(default_factory=list)

    # ---- 关系名解析（CTE 遮蔽基础表的关键） ----

    def find_relation(self, name: str) -> Binding | None:
        """按别名/关系名查找绑定，仅查本作用域（表名不做跨层相关引用）。"""
        if name in self.bindings:
            return self.bindings[name]
        return None

    def find_cte(self, name: str) -> DerivedBinding | None:
        """沿作用域链查找 CTE；内层 CTE 遮蔽外层同名 CTE 与基础表。"""
        if name in self.ctes:
            return self.ctes[name]
        if self.parent is not None:
            return self.parent.find_cte(name)
        return None

    # ---- 列名解析 ----

    def resolve_table_name(self, table: str) -> Binding | None:
        binding = self.find_relation(table)
        if binding is not None:
            return binding
        cte = self.find_cte(table)
        if cte is not None:
            return cte
        return None

    def resolve_column(
        self,
        column: exp.Column,
        path: tuple[str, ...],
    ) -> ExprSources:
        """把一个 Column 节点解析为重定基后的来源列表。

        限定列（``t.c``）只在对应绑定内查找；未限定列按"内层优先、外层兜底"
        的作用域规则收集候选，若同一层有多个绑定同时命中则报歧义。
        """
        loc = self._locate(column)
        if column.table:
            binding = self._resolve_qualified_table(column.table, loc)
            sources = binding.resolve_column(column.name, path)
            if not sources.value and not sources.control:
                raise UnknownColumnError(
                    f"{column.table}.{column.name}", loc
                )
            return sources

        return self._resolve_unqualified(column.name, path, loc)

    def _locate(self, node: exp.Expression) -> ErrorLocation | None:
        if self.positions is None:
            return None
        return self.positions.location_of(node)

    def _resolve_qualified_table(
        self, table: str, loc: ErrorLocation
    ) -> Binding:
        scope: Scope | None = self
        while scope is not None:
            binding = scope.resolve_table_name(table)
            if binding is not None:
                return binding
            scope = scope.parent
        raise UnknownColumnError(table, loc)

    def _resolve_unqualified(
        self,
        name: str,
        path: tuple[str, ...],
        loc: ErrorLocation,
    ) -> ExprSources:
        # 1) 本作用域内逐绑定收集命中；多个绑定命中 -> 歧义
        hits: list[tuple[str, ExprSources]] = []
        for alias in sorted(self.bindings):
            binding = self.bindings[alias]
            if name in binding.names:
                hits.append((alias, binding.resolve_column(name, path)))
        if len(hits) > 1:
            raise AmbiguousColumnError(
                name,
                [f"{alias}.{name}" for alias, _ in hits],
                loc,
            )
        if len(hits) == 1:
            return hits[0][1]

        # 2) 本作用域 CTE 的输出列（CTE 遮蔽同名基础表）
        for cte_name in sorted(self.ctes):
            cte = self.ctes[cte_name]
            if name in cte.names:
                return cte.resolve_column(name, path)

        # 3) 投影别名（HAVING/QUALIFY/ORDER BY 可引用本层派生列）
        if name in self.projection_aliases:
            inner = self.projection_aliases[name]
            return ExprSources(
                value=[
                    Source(ref=s.ref, kind=s.kind, path=path + s.path)
                    for s in inner["value"]
                ],
                control=[
                    Source(ref=s.ref, kind=s.kind, path=path + s.path)
                    for s in inner["control"]
                ],
            )

        # 4) 外层作用域（相关子查询）
        if self.parent is not None:
            return self.parent._resolve_unqualified(name, path, loc)

        raise UnknownColumnError(name, loc)


def location_of(node: exp.Expression) -> ErrorLocation | None:
    """从 sqlglot 节点提取 1 起始行列号。"""
    meta = getattr(node, "meta", {}) or {}
    line = meta.get("line")
    col = meta.get("col")
    if not line:
        return None
    snippet = node.sql()
    return ErrorLocation(line=line, col=col, snippet=snippet)
