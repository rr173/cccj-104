"""作用域与绑定。

每个 SELECT（含子查询里的 SELECT）拥有一个 :class:`Scope`。作用域链通过
``parent`` 连接，名字解析沿链向上查找，从而自然支持相关子查询引用外层绑定。

绑定分两种：

* :class:`TableBinding`：目录中的基础表，列解析为 ``catalog.schema.table.column``；
* :class:`DerivedBinding`：子查询/CTE 等派生关系，列解析结果需要 *重定基*
  （rebase）到派生关系内部字段的基础身份，而不是把两层同名别名拼成假边。

来源分三桶（见 :mod:`lineage.model`）：value（值计算）、control（匹配/选择/
分组/排序）、existence（行集是否为空）。三者语义不同，跨层传递时保持分桶。
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

    ``inner_fields`` 记录派生关系每个输出列名 -> 该列内部已算出的三桶来源；
    解析外层引用时，把内层来源的路径前置外层访问路径，完成重定基。

    ``row_filters`` 是关系级 *匹配/选择* 来源：外层即使一个内层列都不引用
    （COUNT(*)、固定标记、只取连接对侧列），这些约束仍须上带。

    ``row_existence`` 是关系级 *行集为空* 来源，例如内层 WHERE 里的 EXISTS。
    ``row_sets`` 是外层按行计数/判空时真正依赖的基数行集；恒单行关系
    （无分组的聚合 / 无 FROM 且无 WHERE 的常量查询）在这里为空。
    """

    alias: str
    column_names: list[str]
    # name -> {"value": [...], "control": [...], "existence": [...]}
    inner_fields: dict[str, dict[str, list[Source]]]
    # 外层 COUNT(*) 等行计数/判空依赖的基数行集标记
    row_sets: list[Source] = field(default_factory=list)
    # 关系级"行能否存在/被选中"的匹配来源
    row_filters: list[Source] = field(default_factory=list)
    # 关系级谓词内部的存在性来源（例如 WHERE 里的 EXISTS 依赖的行集）
    row_existence: list[Source] = field(default_factory=list)
    # 该派生关系输出行数恒为 1（与底层表行数无关）
    constant_cardinality: bool = False
    # 恒单行且空输入时投影值变 NULL（SUM/MIN/MAX 等；COUNT 为空输入返回 0）
    null_on_empty_value: bool = False

    @property
    def names(self) -> list[str]:
        return self.column_names

    def resolve_column(
        self,
        column: str,
        path: tuple[str, ...],
    ) -> ExprSources:
        """重定基时严格保留内层的 value/control/existence 分桶。"""
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
            existence=[
                Source(ref=s.ref, kind=s.kind, path=path + s.path)
                for s in inner["existence"]
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

    # -- 关系级来源 -------------------------------------------------------
    # 本 SELECT 自身 WHERE/JOIN ON/HAVING/QUALIFY 谓词的匹配来源
    predicate_control: list[Source] = field(default_factory=list)
    # 上述谓词内部的存在性来源（例如 WHERE 里的 EXISTS 依赖的行集）
    predicate_existence: list[Source] = field(default_factory=list)
    # GROUP BY 的匹配来源（分组键影响行数与取值，不决定"是否为空"）
    group_control: list[Source] = field(default_factory=list)
    # FROM/JOIN 中派生关系上带的匹配/存在性来源
    inherited_control: list[Source] = field(default_factory=list)
    inherited_existence: list[Source] = field(default_factory=list)
    # 汇总后分发到本层全部输出字段的关系级来源（匹配 / 存在性）
    relation_control: list[Source] = field(default_factory=list)
    relation_existence: list[Source] = field(default_factory=list)
    # 聚合后的行过滤（HAVING/QUALIFY）：即使恒单行也可能把该行过滤掉
    post_aggregate_control: list[Source] = field(default_factory=list)
    post_aggregate_existence: list[Source] = field(default_factory=list)
    # 派生关系上带时真正"决定输出行能否存在"的来源（恒单行关系已剔除
    # 只改变聚合输入的 WHERE/JOIN ON）
    row_select_control: list[Source] = field(default_factory=list)
    row_select_existence: list[Source] = field(default_factory=list)
    # 本关系底层数据的行集标记（某一基础关系是否还有行）
    row_sets: list[Source] = field(default_factory=list)
    # 恒单行关系（无 GROUP BY/HAVING/QUALIFY/LIMIT 的聚合，或无 FROM 且无
    # WHERE 的常量查询）：输出行数恒为 1，与底层表行数无关。
    constant_cardinality: bool = False
    # 恒单行关系在"输入集合为空"时，投影值是否会从具体值变成 NULL
    # （SUM/MIN/MAX/AVG 为真；COUNT 为假，空输入返回 0）。
    null_on_empty_value: bool = False

    # ---- 对外的语义接口 ----

    def exists_sources(self) -> ExprSources:
        """EXISTS/NOT EXISTS 整体的来源：只问"谓词能否找到记录"。

        * 恒单行关系（如 ``SELECT COUNT(*) FROM orders``）：无论底层表是否有
          行 EXISTS 都是真 -> 没有任何来源；SELECT 列表中的列只是占位，不参与
          筛选，绝不被当成影响结果的因素。
        * 其余情况：谓词里的匹配来源进 control，来自 WHERE 的存在性来源与
          底层行集标记进 existence（删空表会改变 EXISTS 结果）。
        """
        if self.constant_cardinality:
            return ExprSources()
        # GROUP BY 键不算 EXISTS 来源：只要输入有行，分组后至少有一个组；
        # 分组键取值本身不改变"能否找到记录"（它只影响标量/IN 的取值）。
        return ExprSources(
            control=list(self.inherited_control) + list(self.predicate_control),
            existence=list(self.relation_existence) + list(self.row_sets),
        )

    def scalar_extra_existence(self) -> list[Source]:
        """标量子查询"输入集合变空 -> 结果变 NULL/消失"的存在性来源。

        非恒单行关系：输入集合变空则不再有输出行，标量结果变成 NULL，行集
        必须可追溯。恒单行关系：聚合仍返回一行，但 SUM/MIN/MAX/AVG 等空输入
        返回 NULL（这段 NULL 变化必须能追到行集）；COUNT 空输入返回 0、其行集
        已是 value 来源，不构成 NULL 变化，不重复上报。
        """
        if not self.constant_cardinality:
            return list(self.row_sets)
        return list(self.row_sets) if self.null_on_empty_value else []

    def in_sources(self) -> ExprSources:
        """IN (subquery) 的成员判断来源。

        与 EXISTS 不同，IN 会读取子查询投影列的 *取值* 来做成员比较，因此投影
        列（value + control）属于 control（匹配过滤）；底层行集是否还有行属于
        existence。
        """
        control: list[Source] = []
        existence: list[Source] = []
        for item in self.output_fields:
            control.extend(item["value"])
            control.extend(item["control"])
            existence.extend(item["existence"])
        if not self.constant_cardinality:
            existence.extend(self.row_sets)
        return ExprSources(control=control, existence=existence)

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
            if not sources.value and not sources.control and not sources.existence:
                raise UnknownColumnError(
                    f"{column.table}.{column.name}", loc
                )
            return sources

        return self._resolve_unqualified(column.name, path, loc)

    def locate(self, node: exp.Expression) -> ErrorLocation | None:
        return self._locate(node)

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
                existence=[
                    Source(ref=s.ref, kind=s.kind, path=path + s.path)
                    for s in inner["existence"]
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
