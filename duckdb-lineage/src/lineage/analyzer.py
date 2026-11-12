"""血缘分析主编排。

分析流程：

1. sqlglot DuckDB 方言解析，失败 -> ``parse_error``（unsupported）；
2. 前置特性检查：递归 CTE、动态 SQL、DML/DDL、LATERAL/UNNEST、非 UNION ALL
   集合运算等 -> 明确 ``unsupported``，不产出半截血缘；
3. 为每个 SELECT 构造 :class:`Scope`，按 CTE 文本顺序分析 CTE（内层同名 CTE
   遮蔽外层及基础表），FROM/JOIN 建立绑定，子查询递归建子作用域；
4. 投影表达式拆 value/control，WHERE/HAVING/JOIN ON/QUALIFY 全部归 control；
   关系层谓词额外记入 ``row_filters``，随派生关系独立上带——即使外层不引用
   任何内层列（COUNT(*)/固定标记/只取连接对侧列），"行能否进入结果"的约束
   也不会丢；表达式级条件（如 CASE WHEN）不进 ``row_filters``，不污染相邻字段；
5. UNION ALL 按位置合并分支。
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
from .model import FieldLineage, LineageResult, Source, row_set_marker
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
    row_filters: list[Source] = field(default_factory=list)
    row_sets: list[Source] = field(default_factory=list)

    def existence_sources(self) -> list[Source]:
        out: list[Source] = list(self.row_filters)
        for item in self.output_fields:
            out.extend(item["value"])
            out.extend(item["control"])
        out.extend(self.row_sets)
        return out


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
        # 作为本层 row_filters 继续上带。基础表绑定没有 row_filters。
        for alias in sorted(scope.bindings):
            binding = scope.bindings[alias]
            if isinstance(binding, DerivedBinding):
                scope.row_filters.extend(
                    Source(ref=s.ref, kind=s.kind, path=s.path)
                    for s in binding.row_filters
                )

        # 行集标记（COUNT(*) 透传）
        scope.row_sets = self._collect_row_sets(scope)

        # 表达式中的子查询作用域（EXISTS / 标量子查询 / IN 子查询）
        self._register_child_scopes(node, scope)

        analyzer = ExpressionAnalyzer(scope)

        # 投影
        self._analyze_projections(node, scope, analyzer)

        # 关系层过滤条件 -> control。这些谓词决定本 SELECT 每一行是否存在，
        # 与外层最终选中了哪个输出列无关，因此单独记为 row_filters：
        # 1) 分发给本层全部输出字段（任一字段的行都受同一谓词约束）；
        # 2) 随 DerivedBinding 独立上带，即使外层一个内层列都不引用也不丢。
        control_paths: list[tuple[exp.Expression, str]] = []
        if node.args.get("where") is not None:
            control_paths.append((node.args["where"], "where"))
        for join in node.args.get("joins") or []:
            on_expr = join.args.get("on")
            if on_expr is not None:
                control_paths.append((on_expr, "join_on"))
        if node.args.get("having") is not None:
            control_paths.append((node.args["having"], "having"))
        if node.args.get("qualify") is not None:
            control_paths.append((node.args["qualify"], "qualify"))
        for cond, label in control_paths:
            sources = analyzer.analyze(cond, (label,))
            # 关系层谓词整体是"选择条件"：子表达式无论默认桶归属，统一归 control
            controlled = ExpressionAnalyzer._as_control(sources)
            scope.row_filters.extend(controlled.control)

        relation_control = list(scope.row_filters)
        for item in scope.output_fields:
            item["control"].extend(
                Source(ref=s.ref, kind=s.kind, path=s.path)
                for s in relation_control
            )

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
                }
            )
        return RelationResult(
            output_fields=merged,
            row_filters=left.row_filters + right.row_filters,
            row_sets=left.row_sets + right.row_sets,
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
            }
        binding = DerivedBinding(
            alias=name,
            column_names=names,
            inner_fields=inner_fields,
            row_sets=list(body_scope.row_sets),
            row_filters=[
                Source(
                    ref=s.ref,
                    kind=s.kind,
                    path=("relation_filter", f"cte:{name}") + s.path,
                )
                for s in body_scope.row_filters
            ],
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
                }
                for item in inner.output_fields
            }
            scope.bindings[alias] = DerivedBinding(
                alias=alias,
                column_names=names,
                inner_fields=inner_fields,
                row_sets=list(inner.row_sets),
                row_filters=[
                    Source(
                        ref=s.ref,
                        kind=s.kind,
                        path=("relation_filter", f"derived:{alias}") + s.path,
                    )
                    for s in inner.row_filters
                ],
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
            }
            scope.output_fields.append(
                {
                    "name": name,
                    "value": list(sources.value),
                    "control": list(sources.control),
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
                }
            )

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
