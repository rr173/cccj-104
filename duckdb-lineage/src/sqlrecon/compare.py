"""多重集合对账核心：分组、逐值比较与一对一最大匹配。

语义约定（与需求逐条对应）：

* **忽略返回顺序、保留重复行数**：结果按完整行的多重集合比较，行不排序去重。
* **分组**：清单给了业务键就按键值成组，键不是唯一键、同键可对应多行；
  NULL 键正常成组（NULL == NULL 在分组语义里成立）；不给键则整张结果当
  一个组。
* **键列始终精确**；非键列默认精确，清单可对数值列给 (abs, rel) 容差，
  判定式 ``|x-y| <= abs + rel * max(|x|, |y|)``。
* **精确数值不经过 float**：Python int / decimal.Decimal 的比较与容差运算
  全部用 Decimal 精确十进制；只有 FLOAT/DOUBLE 列才按 IEEE 浮点运算。
* NULL 只等于 NULL；NaN 只等于 NaN；+Inf/-Inf 分别只等于自身。
* 组内用 **二分图最大匹配（Hopcroft-Karp）** 找一对一配对：每行至多与
  对面一行配对，一行不能复用抵多行；多个非键列必须由 *同一对行* 同时满足，
  不做各列独立排序。贪心可能误判的重复键容差场景因此得到正确结论。
* 配不满时只报两边剩余行（含重复次数、键与全部原始列值），不拼"变化行"。
* 全部输出按编码后的行值做确定性排序，输入行顺序变化不影响结论、配对数
  与差异展示。
"""

from __future__ import annotations

import decimal
import math
from dataclasses import dataclass, field
from typing import Any

from .encoding import encode_row, encode_scalar
from .errors import SchemaMismatchError
from .manifest import CheckSpec, Tolerance
from .runner import ColumnMeta, QueryResult, validate_named_columns, validate_schema

_INTEGER_IDS = {
    "tinyint", "smallint", "integer", "bigint", "hugeint",
    "utinyint", "usmallint", "uinteger", "ubigint",
}
_FLOAT_IDS = {"float", "double"}
_DECIMAL_ID = "decimal"


# ---------------------------------------------------------------------------
# 计划：把清单规则绑定到实际输出列
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnPlan:
    index: int
    name: str
    type_text: str
    type_id: str
    is_key: bool
    tolerance: Tolerance | None


@dataclass(frozen=True)
class ComparisonPlan:
    check_name: str
    columns: tuple[ColumnPlan, ...]
    key_indexes: tuple[int, ...]
    value_indexes: tuple[int, ...]
    grouped: bool

    def type_ids(self) -> tuple[str | None, ...]:
        return tuple(c.type_id for c in self.columns)


def build_plan(spec: CheckSpec, old: QueryResult, new: QueryResult) -> ComparisonPlan:
    """核对列结构并把键/容差规则绑定到列下标。"""
    columns = validate_schema(old, new, check_name=spec.name)
    validate_named_columns(
        columns,
        spec.keys,
        tuple(spec.tolerance),
        check_name=spec.name,
    )
    plans: list[ColumnPlan] = []
    for idx, col in enumerate(columns):
        is_key = col.name in spec.keys
        tol = None if is_key else spec.tolerance.get(col.name)
        plans.append(
            ColumnPlan(
                index=idx,
                name=col.name,
                type_text=col.type_text,
                type_id=col.type_id,
                is_key=is_key,
                tolerance=tol,
            )
        )
    return ComparisonPlan(
        check_name=spec.name,
        columns=tuple(plans),
        key_indexes=tuple(i for i, c in enumerate(plans) if c.is_key),
        value_indexes=tuple(i for i, c in enumerate(plans) if not c.is_key),
        grouped=spec.grouped,
    )


# ---------------------------------------------------------------------------
# 标量比较
# ---------------------------------------------------------------------------


def _exact_equal(left: Any, right: Any, type_id: str) -> bool:
    """同类型列上的精确相等。

    整数与 DECIMAL 走 Python int/Decimal 的精确相等；float 显式处理
    NaN/±Inf（NaN 只等于 NaN，无穷分符号）；NULL 只等于 NULL。
    """
    if left is None or right is None:
        return left is None and right is None

    if type_id in _FLOAT_IDS:
        if math.isnan(left) or math.isnan(right):
            # NaN != NaN 在 IEEE 比较下为假，但对账语义要求 NaN 只和 NaN 相等
            return math.isnan(left) and math.isnan(right)
        if math.isinf(left) or math.isinf(right):
            # +Inf 只等于 +Inf，-Inf 只等于 -Inf
            return left == right
        return left == right

    if type_id in _INTEGER_IDS:
        return isinstance(left, int) and isinstance(right, int) and left == right
    if type_id == _DECIMAL_ID:
        return isinstance(left, decimal.Decimal) and left == right

    # 其余类型（varchar/blob/date/timestamp/复合类型...）用 Python 相等，
    # 不做任何字符串化或隐式类型转换。
    return left == right


def _to_decimal(value: Any) -> decimal.Decimal:
    if isinstance(value, decimal.Decimal):
        return value
    if isinstance(value, int):
        return decimal.Decimal(value)
    # FLOAT/DOUBLE 列不会走这里；拒绝任何隐式 float->Decimal 的精度借用。
    raise SchemaMismatchError(  # pragma: no cover - 由列类型校验前置保证
        f"精确数值列出现非精确类型 {type(value).__name__}",
        reason="unexpected_numeric_type",
    )


def _numeric_tolerant_equal(
    left: Any,
    right: Any,
    type_id: str,
    tol: Tolerance,
) -> bool:
    """数值容差比较；非有限值（NULL/NaN/Inf）退回精确语义。"""
    if left is None or right is None:
        return left is None and right is None

    if type_id in _FLOAT_IDS:
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        if math.isinf(left) or math.isinf(right):
            return left == right
        diff = abs(left - right)
        bound = tol.abs + tol.rel * max(abs(left), abs(right))
        return diff <= bound

    if type_id in _INTEGER_IDS or type_id == _DECIMAL_ID:
        ldec = _to_decimal(left)
        rdec = _to_decimal(right)
        if ldec.is_nan() or rdec.is_nan():
            return ldec.is_nan() and rdec.is_nan()
        if ldec.is_infinite() or rdec.is_infinite():
            return ldec == rdec
        diff = abs(ldec - rdec)
        bound = decimal.Decimal(str(tol.abs)) + decimal.Decimal(str(tol.rel)) * max(
            abs(ldec), abs(rdec)
        )
        return diff <= bound

    # 前置校验已保证容差列是数值列；走到这里属于内部错误。
    raise SchemaMismatchError(
        f"容差列出现非数值逻辑类型 {type_id}",
        reason="tolerance_on_non_numeric",
    )


def cells_compatible(
    left_row: tuple,
    right_row: tuple,
    plan: ComparisonPlan,
) -> bool:
    """两行是否可配对：所有非键列同时满足各自规则（键在分组时已相等）。"""
    for col in plan.columns:
        if col.is_key:
            if not _exact_equal(left_row[col.index], right_row[col.index], col.type_id):
                return False
            continue
        if col.tolerance is None:
            if not _exact_equal(left_row[col.index], right_row[col.index], col.type_id):
                return False
        else:
            if not _numeric_tolerant_equal(
                left_row[col.index], right_row[col.index], col.type_id, col.tolerance
            ):
                return False
    return True


# ---------------------------------------------------------------------------
# 分组（NULL 键正常成组）
# ---------------------------------------------------------------------------


def _key_of(row: tuple, plan: ComparisonPlan) -> tuple:
    return tuple(row[i] for i in plan.key_indexes)


def _group_rows(rows: list[tuple], plan: ComparisonPlan) -> dict[tuple, list[int]]:
    groups: dict[tuple, list[int]] = {}
    for idx, row in enumerate(rows):
        key = _key_of(row, plan) if plan.grouped else ()
        groups.setdefault(key, []).append(idx)
    return groups


# ---------------------------------------------------------------------------
# Hopcroft-Karp 一对一最大匹配（左/右展开为行实例，重复行各占一个节点）
# ---------------------------------------------------------------------------


def maximum_matching(
    left_idx: list[int],
    right_idx: list[int],
    rows_left: list[tuple],
    rows_right: list[tuple],
    plan: ComparisonPlan,
) -> dict[int, int]:
    """返回组内最大一对一匹配 {左行下标: 右行下标}。

    边由 :func:`cells_compatible` 判定，多个非键列在同一对行上同时成立才算
    一条边。Hopcroft-Karp 与遍历顺序无关地给出最大匹配 *规模*；为了让差异
    展示也不依赖输入行顺序，建图前先按行的规范签名把两边节点排序，使图的
    拓扑（含并列时的选择）只由行值决定。剩余集合再按行值聚合，因此同样的数据
    任意换序重跑，结论、匹配数量与差异展示完全一致。
    """
    # 规范排序：重复值的行互为等价节点，排序键只含行值不含原始下标。
    left_sorted = sorted(left_idx, key=lambda i: _row_code(rows_left[i], plan))
    right_sorted = sorted(right_idx, key=lambda i: _row_code(rows_right[i], plan))
    n_left = len(left_sorted)
    n_right = len(right_sorted)
    adj: list[list[int]] = []
    for li in left_sorted:
        edge: list[int] = []
        for rj, ri in enumerate(right_sorted):
            if cells_compatible(rows_left[li], rows_right[ri], plan):
                edge.append(rj)
        adj.append(edge)

    pair_l = [-1] * n_left
    pair_r = [-1] * n_right
    inf = n_left + n_right + 1
    dist = [0] * (n_left + 1)

    def bfs() -> bool:
        queue: list[int] = []
        for u in range(n_left):
            if pair_l[u] == -1:
                dist[u] = 0
                queue.append(u)
            else:
                dist[u] = inf
        dist[n_left] = inf
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            if dist[u] < dist[n_left]:
                for v in adj[u]:
                    pu = pair_r[v]
                    if dist[pu if pu != -1 else n_left] == inf:
                        target = pu if pu != -1 else n_left
                        dist[target] = dist[u] + 1
                        if pu != -1:
                            queue.append(pu)
        return dist[n_left] != inf

    def dfs(u: int) -> bool:
        if u == n_left:
            return True
        for v in adj[u]:
            pu = pair_r[v]
            target = pu if pu != -1 else n_left
            if dist[target] == dist[u] + 1 and (pu == -1 or dfs(pu)):
                pair_l[u] = v
                pair_r[v] = u
                return True
        dist[u] = inf
        return False

    matching = 0
    while bfs():
        for u in range(n_left):
            if pair_l[u] == -1 and dfs(u):
                matching += 1

    result: dict[int, int] = {}
    for u, v in enumerate(pair_l):
        if v != -1:
            result[left_sorted[u]] = right_sorted[v]
    return result


# ---------------------------------------------------------------------------
# 差异展示（确定性：按完整编码行聚合与排序）
# ---------------------------------------------------------------------------


def _row_code(row: tuple, plan: ComparisonPlan) -> tuple:
    """行的确定性签名：JSON 可序列化的编码元组，可排序、可比较。"""
    return tuple(
        _stable_token(part) for part in encode_row(row, plan.type_ids())
    )


def _stable_token(encoded: dict[str, Any]) -> tuple:
    return (encoded["t"], _json_sort_key(encoded["v"]))


def _json_sort_key(value: Any) -> Any:
    if isinstance(value, dict):
        return ("__dict__", tuple(sorted((k, _json_sort_key(v)) for k, v in value.items())))
    if isinstance(value, list):
        return ("__list__", tuple(_json_sort_key(v) for v in value))
    # None < bool < number < str 的稳定全序；不同类型不会在同列混排，
    # 跨类型只在极少数保底编码里出现，给一个确定顺序即可。
    return (type(value).__name__, value)


def _encoded_row(row: tuple, plan: ComparisonPlan) -> dict[str, Any]:
    return {
        col.name: encode_scalar(row[col.index], col.type_id)
        for col in plan.columns
    }


def _leftover_summary(
    indexes: list[int],
    rows: list[tuple],
    plan: ComparisonPlan,
) -> list[dict[str, Any]]:
    """把剩余行按完整行值聚合成 {row, count}，按编码签名确定性排序。"""
    buckets: dict[tuple, dict[str, Any]] = {}
    for idx in indexes:
        code = _row_code(rows[idx], plan)
        if code not in buckets:
            buckets[code] = {"row": _encoded_row(rows[idx], plan), "count": 0}
        buckets[code]["count"] += 1
    return [buckets[code] for code in sorted(buckets)]


def _key_encoded(key: tuple, plan: ComparisonPlan) -> dict[str, Any]:
    return {
        plan.columns[idx].name: encode_scalar(
            key[pos], plan.columns[idx].type_id
        )
        for pos, idx in enumerate(plan.key_indexes)
    }


# ---------------------------------------------------------------------------
# 顶层比较
# ---------------------------------------------------------------------------


@dataclass
class GroupDifference:
    key: dict[str, Any]
    left_count: int
    right_count: int
    matched_pairs: int
    left_unmatched: list[dict[str, Any]] = field(default_factory=list)
    right_unmatched: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "left_rows": self.left_count,
            "right_rows": self.right_count,
            "matched_pairs": self.matched_pairs,
            "left_unmatched": self.left_unmatched,
            "right_unmatched": self.right_unmatched,
        }


@dataclass
class ComparisonOutcome:
    check_name: str
    equal: bool
    total_left_rows: int
    total_right_rows: int
    total_matched_pairs: int
    group_count: int
    differences: list[GroupDifference]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "equal" if self.equal else "difference",
            "left_rows": self.total_left_rows,
            "right_rows": self.total_right_rows,
            "matched_pairs": self.total_matched_pairs,
            "groups_total": self.group_count,
            "groups_with_difference": len(self.differences),
            "differences": [d.to_dict() for d in self.differences],
        }


def compare(spec: CheckSpec, old: QueryResult, new: QueryResult) -> ComparisonOutcome:
    """执行一次完整对账，返回结构化结论（不抛"差异"异常）。"""
    plan = build_plan(spec, old, new)
    left_groups = _group_rows(old.rows, plan)
    right_groups = _group_rows(new.rows, plan)

    all_keys = set(left_groups) | set(right_groups)
    differences: list[GroupDifference] = []
    total_matched = 0
    group_total = 0

    # 组顺序按键的编码签名确定，NULL 键（None 值）也参与稳定排序。
    ordered_keys = sorted(
        all_keys,
        key=lambda k: tuple(_json_sort_key(v) for v in k) if k else (),
    )
    for key in ordered_keys:
        group_total += 1
        left_idx = list(left_groups.get(key, ()))
        right_idx = list(right_groups.get(key, ()))
        match = maximum_matching(left_idx, right_idx, old.rows, new.rows, plan)
        total_matched += len(match)

        matched_left = set(match)
        matched_right = set(match.values())
        left_left = [i for i in left_idx if i not in matched_left]
        right_left = [i for i in right_idx if i not in matched_right]

        if left_left or right_left:
            differences.append(
                GroupDifference(
                    key=_key_encoded(key, plan) if plan.grouped else {},
                    left_count=len(left_idx),
                    right_count=len(right_idx),
                    matched_pairs=len(match),
                    left_unmatched=_leftover_summary(left_left, old.rows, plan),
                    right_unmatched=_leftover_summary(right_left, new.rows, plan),
                )
            )

    return ComparisonOutcome(
        check_name=spec.name,
        equal=not differences,
        total_left_rows=len(old.rows),
        total_right_rows=len(new.rows),
        total_matched_pairs=total_matched,
        group_count=group_total,
        differences=differences,
    )
