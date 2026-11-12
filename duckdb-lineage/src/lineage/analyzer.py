"""血缘分析主编排。

分析流程：

1. sqlglot DuckDB 方言解析，失败 -> ``parse_error``（unsupported）；
2. 前置特性检查：递归 CTE、动态 SQL、DML/DDL、LATERAL/UNNEST、非 UNION ALL
   集合运算等 -> 明确 ``unsupported``，不产出半截血缘；
3. 为每个 SELECT 构造 :class:`Scope`，按 CTE 文本顺序分析 CTE（内层同名 CTE
   遮蔽外层及基础表），FROM/JOIN 建立绑定，子查询递归建子作用域；
4. 投影表达式拆 value/control/existence 三桶；WHERE/HAVING/JOIN ON/QUALIFY
   是关系层 *匹配* 谓词，归 control，并分发到本层全部输出字段；谓词内部的
   行集来源归 existence；
5. 关系层谓词是否还构成"行能否存在"的约束，取决于关系的基数语义：
   * 无 GROUP BY 的聚合（含无 FROM 的常量查询）输出行数恒为 1，底层行级
     WHERE 只改变聚合取值、不会让输出行消失 -> 不作为派生关系的行选择来源
     上带，避免把内层筛选扩散成外层 `COUNT(*)` 等消费方的行选择来源；这类
     关系自身的 ``row_sets`` 也为空（行数不随底层行数变）；
   * 普通 SELECT 的 WHERE/JOIN ON 才进入 ``row_filters`` 随派生关系独立
     上带：即使外层不引用任何内层列（COUNT(*)/固定标记/只取连接对侧列），
     约束也不会丢；
   * 表达式级条件（如 CASE WHEN）不进 ``row_filters``，不污染相邻字段；
6. UNION ALL 按位置合并分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError as SqlglotParseError

from .catalog import Catalog
from .errors import (
    AmbiguousColumnError,
    LineageError,
    LineageParseError,
    LineageUnsupported,
    UnknownColumnError,
    UnknownRelationError,
)
from .expressions import ExpressionAnalyzer
from .model import ExprSources, FieldLineage, LineageResult, Source, row_set_marker
from .positions import PositionMap
from .scope import DerivedBinding, Scope, TableBinding, location_of

DIALECT = "duckdb"

# sqlglot 对 Command 等回退解析会打 warning，错误结果已由本模块结构化输出，
# 避免警告污染下游 JSON 管道。
logging.getLogger("sqlglot").setLevel(logging.ERROR)


@dataclass
class RelationResult:
    """UNION 等非 SELECT 关系节点的分析结果（与 Scope 同构的最小接口）。"""

    output_fields: list[dict] = field(default_factory=list)
    relation_control: list[Source] = field(default_factory=list)
    relation_existence: list[Source] = field(default_factory=list)
    row_sets: list[Source] = field(default_factory=list)
    constant_cardinality: bool = False
    null_on_empty_value: bool = False

    def exists_sources(self) -> ExprSources:
        if self.constant_cardinality:
            return ExprSources()
        return ExprSources(
            control=list(self.relation_control),
            existence=list(self.relation_existence) + list(self.row_sets),
        )

    def in_sources(self) -> ExprSources:
        control = list(self.relation_control)
        existence = list(self.relation_existence)
        for item in self.output_fields:
            control.extend(item["value"])
            control.extend(item["control"])
            existence.extend(item["existence"])
        if not self.constant_cardinality:
            existence.extend(self.row_sets)
        return ExprSources(control=control, existence=existence)

    def scalar_extra_existence(self) -> list[Source]:
        if not self.constant_cardinality:
            return list(self.row_sets)
        return list(self.row_sets) if self.null_on_empty_value else []


class LineageAnalyzer:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._positions: PositionMap | None = None

    # 入口 -------------------------------------------------------------------

    def analyze(
        self,
        sql: str,
        sql_file: str | None = None,
        top_relation: str = "<top_query>",
    ) -> LineageResult:
        try:
            statements = sqlglot.parse(sql, read=DIALECT)
        except SqlglotParseError as ex:
            return self._fail(self._parse_error_message(ex), sql_file)

        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            err = LineageUnsupported(
                "仅支持单条查询语句，多语句脚本请拆分后分析"
            )
            return self._fail(err, sql_file)
        root = statements[0]

        try:
            self._positions = PositionMap(sql, DIALECT)
            self._precheck(root)
            root_scope = self._build_scope(root, parent=None, relation_name=top_relation)
            fields = [
                FieldLineage(
                    name=item["name"],
                    relation=top_relation,
                    value_sources=list(item["value"]),
                    control_sources=list(item["control"]),
                    existence_sources=list(item["existence"]),
                )
                for item in root_scope.output_fields
            ]
            return LineageResult(
                status="ok",
                dialect=DIALECT,
                sql_file=sql_file,
                output_relation=top_relation,
                fields=fields,
            )
        except LineageError as err:
            return self._fail(err, sql_file)

    # 前置检查 ---------------------------------------------------------------

    def _loc(self, node: exp.Expression):
        """优先使用 token 定位器；未初始化时退回 sqlglot meta。"""
        if self._positions is not None:
            return self._positions.location_of(node)
        return location_of(node)

    def _precheck(self, root: exp.Expression) -> None:
        with_node = root.args.get("with_")
        if with_node is not None and with_node.args.get("recursive"):
            cte = with_node.expressions[0]
            raise LineageUnsupported(
                "递归 CTE 不受支持（递归终止条件与固定点语义无法静态展开为列级血缘）",
                self._loc(cte),
            )

        if isinstance(root, exp.Command):
            raise LineageUnsupported(
                f"动态 SQL / 命令式语句不受支持: {root.sql()[:80]}",
                self._loc(root),
            )
        if not isinstance(root, (exp.Select, exp.Union)):
            raise LineageUnsupported(
                f"仅支持 SELECT 查询，收到 {type(root).__name__}",
                self._loc(root),
            )

        for node in root.walk():
            if isinstance(node, exp.Lateral):
                raise LineageUnsupported(
                    "LATERAL 关联不受支持（逐行派生的列绑定无法静态确定）",
                    self._loc(node),
                )
            if isinstance(node, exp.Unnest):
                raise LineageUnsupported(
                    "UNNEST 表函数不受支持（展开基数与列名依赖运行时数据）",
                    self._loc(node),
                )
            if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
                raise LineageUnsupported(
                    f"写操作 {type(node).__name__} 不在 SELECT 血缘方言范围内",
                    self._loc(node),
                )
            if isinstance(node, (exp.Create, exp.Alter, exp.Drop)):
                raise LineageUnsupported(
                    f"DDL {type(node).__name__} 不在 SELECT 血缘方言范围内",
                    self._loc(node),
                )
            if isinstance(node, exp.Pivot):
                raise LineageUnsupported(
                    "PIVOT 不受支持（输出列依赖运行时取值）",
                    self._loc(node),
                )
            if isinstance(node, exp.Union):
                if node.args.get("distinct"):
                    raise LineageUnsupported(
                        "仅支持 UNION ALL；去重集合运算 UNION/INTERSECT/EXCEPT "
                        "会改变行存在性语义，暂不建模",
                        self._loc(node),
                    )
            if isinstance(node, (exp.Intersect, exp.Except)):
                raise LineageUnsupported(
                    f"{type(node).__name__} 集合运算不受支持（仅支持 UNION ALL）",
                    self._loc(node),
                )
            if isinstance(node, exp.Table) and not node.name:
                raise LineageUnsupported(
                    f"表函数/无名关系不受支持: {node.sql()[:80]}",
                    self._loc(node),
                )

    # 关系 -> 作用域 ----------------------------------------------------------

    def _build_scope(
        self,
        node: exp.Expression,
        parent: Scope | None,
        relation_name: str,
    ) -> Scope | RelationResult:
        if isinstance(node, exp.Union):
            return self._build_union(node, parent, relation_name)
        if not isinstance(node, exp.Select):
            raise LineageUnsupported(
                f"无法为 {type(node).__name__} 构造查询作用域", self._loc(node)
            )

        scope = Scope(
            node=node,
            parent=parent,
            catalog=self.catalog,
            positions=self._positions,
            relation_name=relation_name,
        )

        # 命名窗口 WINDOW w AS (...)：定义节点的名字在 this 标识符上
        for win in node.args.get("windows") or []:
            name = getattr(win.this, "name", None) or win.name
            scope.named_windows[name] = win

        # CTE（按文本顺序；前置 CTE 通过 parent 链对后续 CTE 可见）
        with_node = node.args.get("with_")
        if with_node is not None:
            for cte in with_node.expressions:
                self._register_cte(cte, scope)

        # FROM / JOIN 绑定
        from_node = node.args.get("from_")
        if from_node is not None:
            self._bind_relation(from_node.this, scope)
        for join in node.args.get("joins") or []:
            self._bind_relation(join.this, scope)

        # 派生关系的关系级筛选决定其输出行能否进入本层：即使本层投影一个
        # 内层列都不引用（固定标记 / COUNT(*) / 仅做存在性连接），约束仍须
        # 继续上带。恒单行派生关系（无分组聚合）已由绑定层清空这两类来源。
        for alias in sorted(scope.bindings):
            binding = scope.bindings[alias]
            if isinstance(binding, DerivedBinding):
                scope.inherited_control.extend(
                    Source(ref=s.ref, kind=s.kind, path=s.path)
                    for s in binding.row_filters
                )
                scope.inherited_existence.extend(
                    Source(ref=s.ref, kind=s.kind, path=s.path)
                    for s in binding.row_existence
                )

        # 本关系底层数据的行集标记（是否还有行）
        scope.row_sets = self._collect_row_sets(scope)
        scope.constant_cardinality = self._is_constant_cardinality(
            node, scope
        )
        scope.null_on_empty_value = self._null_on_empty_value(
            node, scope.constant_cardinality
        )

        # 表达式中的子查询作用域（EXISTS / 标量子查询 / IN 子查询）
        self._register_child_scopes(node, scope)

        analyzer = ExpressionAnalyzer(scope)

        # 投影
        self._analyze_projections(node, scope, analyzer)

        # 关系层谓词 -> control（匹配/选择原因）。这些谓词决定本 SELECT
        # 每一行是否被选中，与外层最终选中了哪个输出列无关，因此分发到本层
        # 全部输出字段；同时汇总为关系级来源供派生绑定独立上带。
        # HAVING/QUALIFY 是聚合后的行过滤，单独记录：即使查询恒为单行，
        # 它们仍可能把那一行过滤掉（如 HAVING COUNT(*) > 0）。
        predicate_nodes: list[tuple[exp.Expression, str]] = []
        if node.args.get("where") is not None:
            predicate_nodes.append((node.args["where"], "where"))
        for join in node.args.get("joins") or []:
            on_expr = join.args.get("on")
            if on_expr is not None:
                predicate_nodes.append((on_expr, "join_on"))
        if node.args.get("having") is not None:
            predicate_nodes.append((node.args["having"], "having"))
        if node.args.get("qualify") is not None:
            predicate_nodes.append((node.args["qualify"], "qualify"))
        for cond, label in predicate_nodes:
            sources = analyzer.analyze(cond, (label,))
            # 谓词整体是"选择条件"：子表达式无论默认桶归属，统一归 control
            controlled = ExpressionAnalyzer._as_control(sources)
            scope.predicate_control.extend(controlled.control)
            scope.predicate_existence.extend(controlled.existence)
            if label in ("having", "qualify"):
                scope.post_aggregate_control.extend(controlled.control)
                scope.post_aggregate_existence.extend(controlled.existence)

        # GROUP BY 是分组原因：决定哪些行进入同一分组，归 control
        group = node.args.get("group")
        if group is not None:
            for j, expr in enumerate(group.expressions):
                src = analyzer.analyze(expr, ("group_by", f"[{j}]"))
                scope.group_control.extend(
                    ExpressionAnalyzer._as_control(src).control
                )

        # 分发到本层全部输出字段的关系级来源（匹配 / 存在性）
        scope.relation_control = (
            list(scope.inherited_control)
            + list(scope.predicate_control)
            + list(scope.group_control)
        )
        scope.relation_existence = (
            list(scope.inherited_existence)
            + list(scope.predicate_existence)
        )
        for item in scope.output_fields:
            item["control"].extend(
                Source(ref=s.ref, kind=s.kind, path=s.path)
                for s in scope.relation_control
            )
            item["existence"].extend(
                Source(ref=s.ref, kind=s.kind, path=s.path)
                for s in scope.relation_existence
            )
            # 恒单行聚合在空输入时可能把值变成 NULL（SUM/MIN/MAX/...）：
            # 这条"输入集合为空"的变化必须能追到行集，与标量子查询同因。
            if scope.constant_cardinality and scope.null_on_empty_value:
                item["existence"].extend(
                    Source(ref=s.ref, kind=s.kind, path=s.path)
                    for s in scope.row_sets
                )

        # 派生关系上带的"行能否进入本层输出"的来源：
        # - 普通关系：WHERE/JOIN ON/GROUP BY 都决定行是否存在；
        # - 恒单行聚合：WHERE/JOIN ON 只改变聚合输入、不改变"仍有一行"，
        #   不能作为外层每个字段的行选择来源扩散（HAVING/QUALIFY 例外，
        #   它们在聚合之后做行过滤）。
        if scope.constant_cardinality:
            scope.row_select_control = (
                list(scope.inherited_control)
                + list(scope.post_aggregate_control)
            )
            scope.row_select_existence = list(scope.inherited_existence)
        else:
            scope.row_select_control = list(scope.relation_control)
            scope.row_select_existence = list(scope.relation_existence)

        return scope

    def _build_union(
        self,
        node: exp.Union,
        parent: Scope | None,
        relation_name: str,
    ) -> RelationResult:
        left = self._build_scope(node.left, parent, f"{relation_name}#left")
        right = self._build_scope(node.right, parent, f"{relation_name}#right")
        if len(left.output_fields) != len(right.output_fields):
            raise LineageUnsupported(
                "UNION ALL 两个分支的输出列数不一致，无法按位置合并",
                self._loc(node),
            )
        merged: list[dict] = []
        for i, (lf, rf) in enumerate(zip(left.output_fields, right.output_fields)):
            merged.append(
                {
                    "name": lf["name"],  # 输出列名以左分支为准
                    "value": lf["value"] + rf["value"],
                    "control": lf["control"] + rf["control"],
                    "existence": lf["existence"] + rf["existence"],
                }
            )
        return RelationResult(
            output_fields=merged,
            relation_control=left.relation_control + right.relation_control,
            relation_existence=left.relation_existence
            + right.relation_existence,
            row_sets=left.row_sets + right.row_sets,
            constant_cardinality=(
                left.constant_cardinality and right.constant_cardinality
            ),
            null_on_empty_value=(
                left.null_on_empty_value or right.null_on_empty_value
            ),
        )

    # CTE --------------------------------------------------------------------

    def _register_cte(self, cte: exp.CTE, enclosing: Scope) -> None:
        name = cte.alias
        body_scope = self._build_scope(cte.this, enclosing, name)
        override_cols = self._cte_column_names(cte)
        if override_cols and len(override_cols) != len(body_scope.output_fields):
            raise LineageUnsupported(
                f"CTE {name} 的列别名数({len(override_cols)})与输出列数"
                f"({len(body_scope.output_fields)})不一致",
                self._loc(cte),
            )
        names = override_cols or [f["name"] for f in body_scope.output_fields]
        inner_fields: dict[str, dict] = {}
        for col_name, item in zip(names, body_scope.output_fields):
            inner_fields[col_name] = {
                "value": list(item["value"]),
                "control": list(item["control"]),
                "existence": list(item["existence"]),
            }
        binding = DerivedBinding(
            alias=name,
            column_names=names,
            inner_fields=inner_fields,
            row_sets=self._consumer_row_sets(body_scope),
            row_filters=self._relabel(
                body_scope.row_select_control, ("relation_filter", f"cte:{name}")
            ),
            row_existence=self._relabel(
                body_scope.row_select_existence, ("relation_filter", f"cte:{name}")
            ),
            constant_cardinality=body_scope.constant_cardinality,
            null_on_empty_value=body_scope.null_on_empty_value,
        )
        # 同名 CTE 遮蔽外层 CTE / 基础表：直接放在当前作用域，解析时内层优先
        enclosing.ctes[name] = binding

    @staticmethod
    def _cte_column_names(cte: exp.CTE) -> list[str]:
        alias_node = cte.args.get("alias")
        if alias_node is None:
            return []
        columns = getattr(alias_node, "columns", None)
        if not columns:
            return []
        return [c.name for c in columns]

    # 绑定 -------------------------------------------------------------------

    def _bind_relation(self, rel: exp.Expression, scope: Scope) -> None:
        if isinstance(rel, exp.Table):
            alias = rel.alias or rel.name
            if alias in scope.bindings:
                raise LineageUnsupported(
                    f"作用域内关系别名重复: {alias}", self._loc(rel)
                )
            # CTE 遮蔽同名基础表：FROM/JOIN 引用 CTE 名时复用派生绑定
            cte_binding = scope.find_cte(rel.name)
            if cte_binding is not None and not (rel.db or rel.catalog):
                scope.bindings[alias] = cte_binding
                return
            table = self.catalog.resolve_table(
                rel.name, db=rel.db or None, catalog=rel.catalog or None
            )
            scope.bindings[alias] = TableBinding(alias=alias, table=table)
        elif isinstance(rel, exp.Subquery):
            inner = self._build_scope(
                rel.this, scope, rel.alias or "<derived>"
            )
            alias = rel.alias or f"_derived_{len(scope.bindings) + 1}"
            names = [f["name"] for f in inner.output_fields]
            inner_fields = {
                item["name"]: {
                    "value": list(item["value"]),
                    "control": list(item["control"]),
                    "existence": list(item["existence"]),
                }
                for item in inner.output_fields
            }
            scope.bindings[alias] = DerivedBinding(
                alias=alias,
                column_names=names,
                inner_fields=inner_fields,
                row_sets=self._consumer_row_sets(inner),
                row_filters=self._relabel(
                    inner.row_select_control, ("relation_filter", f"derived:{alias}")
                ),
                row_existence=self._relabel(
                    inner.row_select_existence, ("relation_filter", f"derived:{alias}")
                ),
                constant_cardinality=inner.constant_cardinality,
                null_on_empty_value=inner.null_on_empty_value,
            )
        else:
            raise LineageUnsupported(
                f"不支持的 FROM/JOIN 关系类型: {type(rel).__name__}",
                self._loc(rel),
            )

    def _collect_row_sets(self, scope: Scope) -> list[Source]:
        markers: list[Source] = []
        for alias, binding in scope.bindings.items():
            if isinstance(binding, TableBinding):
                markers.append(
                    Source(
                        ref=row_set_marker(binding.table.fq_name),
                        kind="row_set",
                        path=("from", f"alias:{alias}"),
                    )
                )
            else:
                markers.extend(
                    Source(ref=s.ref, kind=s.kind, path=s.path)
                    for s in binding.row_sets
                )
        return markers

    # 子查询作用域注册 --------------------------------------------------------

    def _register_child_scopes(self, select: exp.Select, scope: Scope) -> None:
        def descend(node: exp.Expression, under_from: bool) -> None:
            # CTE 定义体内层已在 _register_cte 里建过作用域，不能再用外层
            # 作用域重复构建（否则相关引用会按错误的父链解析）。
            if isinstance(node, (exp.With, exp.CTE)):
                return
            if isinstance(node, exp.Subquery):
                if not under_from:
                    scope.child_scopes[id(node.this)] = self._build_scope(
                        node.this, scope, "<subquery>"
                    )
                return  # 子查询内部由其自身作用域负责，剪枝
            if isinstance(node, exp.Exists):
                scope.child_scopes[id(node.this)] = self._build_scope(
                    node.this, scope, "<exists_subquery>"
                )
                return
            next_under = under_from or isinstance(node, (exp.From, exp.Join))
            for child in node.iter_expressions():
                descend(child, next_under)

        descend(select, False)

    # 投影 -------------------------------------------------------------------

    def _analyze_projections(
        self,
        select: exp.Select,
        scope: Scope,
        analyzer: ExpressionAnalyzer,
    ) -> None:
        for index, proj in enumerate(select.expressions):
            # SELECT *
            if isinstance(proj, exp.Star):
                for alias in scope.bindings:
                    self._expand_star(scope, analyzer, scope.bindings[alias], None, index)
                continue
            # SELECT t.*
            if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                binding = scope.resolve_table_name(proj.table)
                if binding is None:
                    raise UnknownColumnError(proj.table, self._loc(proj))
                self._expand_star(scope, analyzer, binding, proj.table, index)
                continue

            sources = analyzer.analyze(proj, ("projection", f"[{index}]"))
            name = self._output_name(proj, index)
            scope.projection_aliases[name] = {
                "value": list(sources.value),
                "control": list(sources.control),
                "existence": list(sources.existence),
            }
            scope.output_fields.append(
                {
                    "name": name,
                    "value": list(sources.value),
                    "control": list(sources.control),
                    "existence": list(sources.existence),
                }
            )

    def _expand_star(
        self,
        scope: Scope,
        analyzer: ExpressionAnalyzer,
        binding,
        qualifier: str | None,
        index: int,
    ) -> None:
        for col_name in binding.names:
            fake_path = ("projection", f"[{index}]", "star_expansion")
            sources = binding.resolve_column(col_name, fake_path)
            scope.output_fields.append(
                {
                    "name": col_name,
                    "value": list(sources.value),
                    "control": list(sources.control),
                    "existence": list(sources.existence),
                }
            )

    @staticmethod
    def _relabel(sources: list[Source], prefix: tuple[str, ...]) -> list[Source]:
        """给关系级来源加前缀，保留原路径（派生关系上带时区分来源层级）。"""
        return [
            Source(ref=s.ref, kind=s.kind, path=prefix + s.path)
            for s in sources
        ]

    @staticmethod
    def _consumer_row_sets(scope) -> list[Source]:
        """外层按行计数/判空时真正依赖的基数行集：恒单行关系不贡献行集。"""
        if scope.constant_cardinality:
            return []
        return list(scope.row_sets)

    def _null_on_empty_value(
        self, select: exp.Select, constant_cardinality: bool
    ) -> bool:
        """恒单行查询在空输入时投影值是否变成 NULL。

        只要投影里有一个空输入返回 NULL 的聚合（SUM/MIN/MAX/AVG/... 而非
        COUNT），或恒单行来自无 FROM 的常量（不涉及空输入），就按"会变 NULL"
        处理。纯 COUNT(*) 的聚合不含 NULL 变化（空输入返回 0）。
        """
        if not constant_cardinality:
            return False
        if select.args.get("from_") is None:
            return False
        projections = [
            p.this if isinstance(p, exp.Alias) else p for p in select.expressions
        ]
        if not any(self._contains_aggregate(p) for p in projections):
            return False
        return any(self._contains_null_on_empty_aggregate(p) for p in projections)

    @classmethod
    def _contains_null_on_empty_aggregate(cls, node: exp.Expression) -> bool:
        if isinstance(node, (exp.Window, exp.Subquery)):
            return False
        if isinstance(node, exp.AggFunc):
            name = node.sql_name().upper()
            return name != "COUNT"
        for key, value in node.args.items():
            if isinstance(value, exp.Expression):
                if cls._contains_null_on_empty_aggregate(value):
                    return True
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, exp.Expression) and (
                        cls._contains_null_on_empty_aggregate(item)
                    ):
                        return True
        return False

    def _is_constant_cardinality(
        self, select: exp.Select, scope: Scope
    ) -> bool:
        """本 SELECT 的输出行数是否恒为 1（与底层表行数无关）。

        条件：没有会改变行数的 GROUP BY / HAVING / QUALIFY / LIMIT，并且
        投影里有顶层聚合（无 GROUP BY 的 COUNT/SUM/...，WHERE 只改变输入
        集合不改变"仍有一行"），或是无 FROM 且无 WHERE 的单行常量查询。

        注意：``SELECT 1 FROM orders`` 是有行数语义的明细查询，不是恒单行；
        ``SELECT COUNT(*) FROM orders WHERE ...`` 则恒为一行（空输入返回 0）。
        """
        if select.args.get("group") is not None:
            return False
        if select.args.get("having") is not None:
            return False
        if select.args.get("qualify") is not None:
            return False
        if select.args.get("limit") is not None:
            return False
        if any(self._has_toplevel_aggregate(p) for p in select.expressions):
            return True
        # 无 FROM 的常量查询：有 WHERE 时可能输出 0 行
        return (
            select.args.get("from_") is None
            and select.args.get("where") is None
        )

    @staticmethod
    def _has_toplevel_aggregate(proj: exp.Expression) -> bool:
        """投影是否含本层聚合（窗口函数/子查询内部的聚合不算）。"""
        node = proj.this if isinstance(proj, exp.Alias) else proj
        return LineageAnalyzer._contains_aggregate(node)

    @staticmethod
    def _contains_aggregate(node: exp.Expression) -> bool:
        if isinstance(node, exp.Window):
            # 窗口函数不折叠行，其内部的聚合不改行数
            return False
        if isinstance(node, exp.Subquery):
            # 子查询有自己的作用域，内部聚合不改本层行数
            return False
        if isinstance(node, exp.AggFunc):
            return True
        for key, value in node.args.items():
            if isinstance(value, exp.Expression):
                if LineageAnalyzer._contains_aggregate(value):
                    return True
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, exp.Expression) and (
                        LineageAnalyzer._contains_aggregate(item)
                    ):
                        return True
        return False

    @staticmethod
    def _output_name(proj: exp.Expression, index: int) -> str:
        if isinstance(proj, exp.Alias):
            return proj.alias
        if isinstance(proj, exp.Column):
            return proj.name
        return f"_col{index + 1}"

    # 错误封装 ---------------------------------------------------------------

    @staticmethod
    def _parse_error_message(ex: SqlglotParseError) -> LineageParseError:
        from .errors import ErrorLocation

        if ex.errors:
            err = ex.errors[0]
            loc = None
            if err.get("line"):
                loc = ErrorLocation(
                    line=err["line"],
                    col=err.get("col"),
                    snippet=err.get("highlight") or "",
                )
            return LineageParseError(err.get("description") or str(ex), loc)
        return LineageParseError(str(ex))

    def _fail(self, err: LineageError, sql_file: str | None) -> LineageResult:
        status = "unsupported"
        if isinstance(err, (AmbiguousColumnError, UnknownColumnError, UnknownRelationError)):
            status = "error"
        return LineageResult(
            status=status,
            dialect=DIALECT,
            sql_file=sql_file,
            output_relation=None,
            fields=[],
            errors=[err.to_payload()],
        )
