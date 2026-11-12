"""表达式级血缘分析。

输出统一为两个来源桶：

* value：表达式 *值* 依赖的来源；
* control：表达式中只起 *条件/选择* 作用的来源。

关键规则：

* CASE 的 WHEN 条件 -> control；THEN/ELSE 分支 -> value。
* 窗口函数的参数 -> value；PARTITION BY / ORDER BY -> control。
* COUNT(*) / COUNT(1) 不伪造全列依赖，记为基础关系的 *行集标记*
  （``row_set:<fq_table>``）；无参 ROW_NUMBER/RANK 同理。
* EXISTS / NOT EXISTS 内部全部来源记为 control（它只决定外层行是否存在）。
* 标量子查询的值来源会向上重定基；IN (subquery) 两侧记 control。
"""

from __future__ import annotations

from sqlglot import exp

from .errors import LineageUnsupported
from .model import ExprSources, Source, row_set_marker
from .scope import Scope

# 无参数（或仅常量参数）聚合/窗口函数：值依赖退化为行集标记
_ROW_SET_AGGREGATES = {
    "COUNT", "ROW_NUMBER", "RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST",
    "NTILE",
}
_AGGREGATE_FUNCS = {
    "SUM", "AVG", "MIN", "MAX", "COUNT", "BOOL_AND", "BOOL_OR",
    "STRING_AGG", "LIST", "LIST_AGG", "ARRAY_AGG", "STDDEV", "STDDEV_POP",
    "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP", "MEDIAN", "QUANTILE_CONT",
    "QUANTILE_DISC", "FIRST", "LAST",
}


class ExpressionAnalyzer:
    """在给定作用域下把表达式拆成 value/control 两个来源桶。"""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    # 对外入口 ---------------------------------------------------------------

    def analyze(self, node: exp.Expression, path: tuple[str, ...]) -> ExprSources:
        return self._visit(node, path)

    # 分发 -------------------------------------------------------------------

    def _visit(self, node: exp.Expression, path: tuple[str, ...]) -> ExprSources:
        if node is None:
            return ExprSources()

        if isinstance(node, exp.Column):
            return self._column(node, path)
        if isinstance(node, exp.Star):
            # 独立 Star 只出现在 SELECT *，星号展开在关系分析层处理
            return ExprSources()
        if isinstance(node, exp.Case):
            return self._case(node, path)
        if isinstance(node, exp.Window):
            return self._window(node, path)
        if isinstance(node, (exp.Exists, exp.Not)):
            # NOT 只包裹 EXISTS/布尔表达式，本身不引入来源
            return self._not_exists(node, path)
        if isinstance(node, exp.Subquery):
            return self._scalar_subquery(node, path)
        if isinstance(node, exp.In):
            return self._in(node, path)
        if isinstance(node, exp.Anonymous):
            return self._anonymous(node, path)
        if isinstance(node, exp.Func):
            return self._func(node, path)
        if isinstance(node, exp.Paren):
            return self._visit(node.this, path + ("paren",))
        if isinstance(node, exp.Alias):
            return self._visit(node.this, path)
        if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
            return ExprSources()

        # 通用回退：递归所有子表达式，来源桶按子节点默认归类
        return self._generic(node, path)

    # 具体节点 ---------------------------------------------------------------

    def _column(self, node: exp.Column, path: tuple[str, ...]) -> ExprSources:
        return self.scope.resolve_column(node, path + ("column", node.name))

    def _case(self, node: exp.Case, path: tuple[str, ...]) -> ExprSources:
        out = ExprSources()
        # CASE <base> WHEN ... 中的 base 也是条件输入 -> control
        if node.this is not None:
            out.extend(
                self._as_control(
                    self._visit(node.this, path + ("case", "base")),
                )
            )
        for i, when in enumerate(node.args.get("ifs", [])):
            cond = self._visit(
                when.this, path + ("case", f"when[{i}]", "condition")
            )
            out.extend(self._as_control(cond))
            result = self._visit(
                when.args.get("true"), path + ("case", f"when[{i}]", "then")
            )
            out.extend(result)
        if node.args.get("default") is not None:
            out.extend(
                self._visit(node.args["default"], path + ("case", "else"))
            )
        return out

    def _window(self, node: exp.Window, path: tuple[str, ...]) -> ExprSources:
        out = ExprSources()
        func = node.this
        func_name = func.sql_name().upper() if hasattr(func, "sql_name") else ""

        # 1) 窗口函数参数 -> value
        args = self._func_args(func)
        if isinstance(func, exp.Count) and isinstance(func.this, exp.Star):
            args = []  # COUNT(*) 特殊处理
        if not args and func_name in _ROW_SET_AGGREGATES:
            out.value.extend(self._row_set_markers(path + ("window", "row_set")))
        for j, arg in enumerate(args):
            if isinstance(arg, exp.Star):
                continue
            out.extend(self._visit(arg, path + ("window", f"arg[{j}]")))

        # 2) 命名窗口引用（SUM(x) OVER w2）：在 SELECT 的 WINDOW 定义中补取
        win_name = self._name_of(node.args.get("alias"))
        named = self.scope.named_windows.get(win_name) if win_name else None

        # 3) PARTITION BY -> control
        partitions = list(node.args.get("partition_by") or [])
        if not partitions and named is not None:
            partitions = list(named.args.get("partition_by") or [])
        for j, part in enumerate(partitions):
            src = self._visit(part, path + ("window", "partition_by", f"[{j}]"))
            out.extend(self._as_control(src))

        # 4) ORDER BY -> control
        order_exprs = self._order_expressions(node, named)
        for j, ordered in enumerate(order_exprs):
            src = self._visit(
                ordered.this, path + ("window", "order_by", f"[{j}]")
            )
            out.extend(self._as_control(src))
        return out

    def _not_exists(
        self, node: exp.Exists | exp.Not, path: tuple[str, ...]
    ) -> ExprSources:
        target = node
        if isinstance(node, exp.Not):
            target = node.this
        if not isinstance(target, exp.Exists):
            # 普通 NOT <boolean expr>：子表达式本身就是条件
            return self._as_control(self._visit(target, path + ("not",)))
        sub = target.this
        sub_scope = self.scope.child_scopes.get(id(sub))
        if sub_scope is None:  # pragma: no cover - 构造期保证已建
            raise LineageUnsupported("EXISTS 子查询作用域未初始化")
        out = ExprSources()
        for src in sub_scope.relation_sources:
            out.control.append(
                Source(
                    ref=src.ref,
                    kind=src.kind,
                    path=path + ("exists",) + src.path,
                )
            )
        return out

    def _scalar_subquery(
        self, node: exp.Subquery, path: tuple[str, ...]
    ) -> ExprSources:
        sub_scope = self.scope.child_scopes.get(id(node.this))
        if sub_scope is None:  # pragma: no cover
            raise LineageUnsupported("标量子查询作用域未初始化")
        out = ExprSources()
        # 标量子查询只允许单列；其字段来源整体重定基为 value
        for field_ in sub_scope.output_fields:
            for src in field_["value"] + field_["control"]:
                out.value.append(
                    Source(ref=src.ref, kind=src.kind, path=path + ("scalar_subquery",) + src.path)
                )
        for src in sub_scope.relation_sources:
            out.control.append(
                Source(ref=src.ref, kind=src.kind, path=path + ("scalar_subquery",) + src.path)
            )
        return out

    def _in(self, node: exp.In, path: tuple[str, ...]) -> ExprSources:
        out = ExprSources()
        # 左侧表达式在 IN 谓词中起选择作用 -> control
        out.extend(
            self._as_control(
                self._visit(node.this, path + ("in", "left"))
            )
        )
        for j, candidate in enumerate(node.expressions):
            if isinstance(candidate, exp.Subquery):
                sub_scope = self.scope.child_scopes.get(id(candidate.this))
                if sub_scope is None:  # pragma: no cover
                    raise LineageUnsupported("IN 子查询作用域未初始化")
                for field_ in sub_scope.output_fields:
                    for src in field_["value"] + field_["control"]:
                        out.control.append(
                            Source(
                                ref=src.ref,
                                kind=src.kind,
                                path=path + ("in", f"subquery[{j}]") + src.path,
                            )
                        )
                for src in sub_scope.relation_sources:
                    out.control.append(
                        Source(
                            ref=src.ref,
                            kind=src.kind,
                            path=path + ("in", f"subquery[{j}]") + src.path,
                        )
                    )
            else:
                out.extend(
                    self._as_control(
                        self._visit(candidate, path + ("in", f"values[{j}]"))
                    )
                )
        return out

    def _anonymous(self, node: exp.Anonymous, path: tuple[str, ...]) -> ExprSources:
        name = node.name.upper()
        if name in _AGGREGATE_FUNCS or name in _ROW_SET_AGGREGATES:
            return self._aggregate_like(node, path, name)
        return self._generic(node, path + ("func", name))

    def _func(self, node: exp.Func, path: tuple[str, ...]) -> ExprSources:
        name = node.sql_name().upper()
        # COUNT(*) / COUNT(1)
        if isinstance(node, exp.Count):
            if isinstance(node.this, exp.Star) or self._is_constant_arg(node.this):
                return ExprSources(
                    value=self._row_set_markers(path + ("aggregate", "COUNT", "row_set"))
                )
        # 无列依赖的聚合（ROW_NUMBER 在窗口分支处理，这里兜底其他 AggFunc）
        if isinstance(node, exp.AggFunc) and self._is_constant_only(node):
            if name in _ROW_SET_AGGREGATES:
                return ExprSources(
                    value=self._row_set_markers(path + ("aggregate", name, "row_set"))
                )
        return self._generic(node, path + ("func", name))

    def _aggregate_like(
        self, node: exp.Expression, path: tuple[str, ...], name: str
    ) -> ExprSources:
        args = self._func_args(node)
        col_args = [
            a for a in args
            if not isinstance(a, (exp.Literal, exp.Boolean, exp.Null, exp.Star))
        ]
        if not col_args and name in _ROW_SET_AGGREGATES:
            return ExprSources(
                value=self._row_set_markers(path + ("aggregate", name, "row_set"))
            )
        out = ExprSources()
        for j, arg in enumerate(args):
            if isinstance(arg, exp.Star):
                continue
            out.extend(self._visit(arg, path + ("aggregate", name, f"arg[{j}]")))
        return out

    # 辅助 -------------------------------------------------------------------

    @staticmethod
    def _name_of(node: object) -> str | None:
        """sqlglot 的 alias 可能是字符串或 Identifier 节点，统一取名字。"""
        if node is None:
            return None
        if isinstance(node, str):
            return node
        name = getattr(node, "name", None)
        return name if name else None

    @staticmethod
    def _func_args(func: exp.Expression) -> list[exp.Expression]:
        """统一提取函数的位置参数（兼容单节点与列表两种 arg 形态）。"""
        raw = func.args.get("this")
        if raw is None:
            return []
        if isinstance(raw, list):
            return [e for e in raw if isinstance(e, exp.Expression)]
        if isinstance(raw, exp.Expression):
            return [raw]
        return []

    def _generic(self, node: exp.Expression, path: tuple[str, ...]) -> ExprSources:
        out = ExprSources()
        for key, child in _iter_children(node):
            if isinstance(child, exp.Expression):
                out.extend(self._visit(child, path + (type(node).__name__, key)))
        return out

    def _order_expressions(
        self,
        node: exp.Window,
        named: exp.Window | None,
    ) -> list[exp.Ordered]:
        order = node.args.get("order")
        if order is None and named is not None:
            order = named.args.get("order")
        if order is None:
            return []
        return list(order.expressions)

    def _row_set_markers(self, path: tuple[str, ...]) -> list[Source]:
        """当前作用域 FROM/JOIN 可见的全部基础关系行集标记。"""
        markers: list[Source] = []
        for alias in sorted(self.scope.bindings):
            binding = self.scope.bindings[alias]
            if hasattr(binding, "table"):
                markers.append(
                    Source(
                        ref=row_set_marker(binding.table.fq_name),
                        kind="row_set",
                        path=path + (f"alias:{alias}",),
                    )
                )
            else:  # 派生关系：透传其内部行集标记
                for src in binding.row_sets:
                    markers.append(
                        Source(ref=src.ref, kind=src.kind, path=path + src.path)
                    )
        return markers

    @staticmethod
    def _as_control(src: ExprSources) -> ExprSources:
        """把一个子表达式的全部来源（含 value）重新归类为 control。"""
        moved = [
            Source(ref=s.ref, kind=s.kind, path=s.path)
            for s in src.value + src.control
        ]
        return ExprSources(control=moved)

    @staticmethod
    def _is_constant_arg(node: exp.Expression | None) -> bool:
        if node is None:
            return True
        return not any(node.find_all(exp.Column)) and not any(
            node.find_all(exp.Subquery)
        )

    @staticmethod
    def _is_constant_only(node: exp.Func) -> bool:
        for child in node.walk():
            if isinstance(child, exp.Column):
                return False
        return True


def _iter_children(node: exp.Expression):
    for key, value in node.args.items():
        if isinstance(value, exp.Expression):
            yield key, value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, exp.Expression):
                    yield key, item
