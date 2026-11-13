"""闭包追踪：从起点出发，只沿子表->父表方向补入必需记录的最小集合。

本模块是 **纯集合算法**，不导入 duckdb：行数据通过 ``fetch_row`` 回调
取得，便于用内存假数据做单元测试，也让演示里的独立对照算法与之形成
真正的两套实现。

规则（与 docs/relslice.md 一致）：

* 先放入全部起点记录；之后对已放入的每条记录，沿每条以它为子表的引用
  补入父记录，直到没有新记录（最小闭包，恰好收敛）；
* 只沿子表->父表方向：带上客户不会带上该客户的其他订单；
* 引用列组中 **任意一列为 NULL** 即视为不需要父记录，跳过该引用；
* 多条路指向同一身份只收一次；自引用、跨表成环都靠"已收不再收"收敛；
* 起点身份在源表中找不到 -> :class:`SeedNotFoundError`；
* 已选中记录引用了不存在的父记录 -> :class:`BrokenReferenceError`，
  载荷带从起点到断口的完整链路（seed + path + 缺失的父身份）；
* 设了预算时，一旦确认必需记录数将超过预算立即抛
  :class:`BudgetExceededError`，不截断、不产出残缺样本。

确定性：调用方传入前应将引用按名字排序、起点按 (表, 身份) 排序；本模块
按 FIFO 队列广度优先处理，每条记录的来历取"先达者"，因此同一逻辑配方
（无论配方文件里表/引用/起点如何排列）得到相同的入选集合与相同的解释
路径。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .errors import BrokenReferenceError, BudgetExceededError, SeedNotFoundError
from .recipe import ReferenceSpec, TableRef

# fetch_row(表, 身份元组) -> 行字典（列名->值）；找不到返回 None
FetchRow = Callable[[TableRef, tuple], dict[str, Any] | None]


@dataclass(frozen=True)
class PathStep:
    """解释链路中的一步：子表记录经某引用指向父表身份。"""

    reference: str
    child: TableRef
    child_identity: tuple
    parent: TableRef
    parent_identity: tuple

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "child": self.child.qname,
            "child_identity": list(self.child_identity),
            "parent": self.parent.qname,
            "parent_identity": list(self.parent_identity),
        }


@dataclass(frozen=True)
class Origin:
    """一条入选记录的来历：它从哪个起点沿哪些引用到达。

    ``seed`` 是回溯到的起点（起点记录指向自身）；``path`` 是从起点记录
    到本记录的引用链，空元组表示本记录就是起点。
    """

    seed: tuple[TableRef, tuple]
    path: tuple[PathStep, ...]


@dataclass
class Closure:
    """追踪结果：各表入选身份 -> 行，以及每条记录的来历。"""

    selected: dict[TableRef, dict[tuple, dict[str, Any]]]
    origins: dict[tuple[TableRef, tuple], Origin]
    total: int

    def identities(self, ref: TableRef) -> list[tuple]:
        """某表入选身份（排序后返回，便于确定性输出）。"""
        return sorted(self.selected.get(ref, {}))


def _trail_payload(origin: Origin) -> dict[str, Any]:
    seed_ref, seed_identity = origin.seed
    return {
        "seed": {"table": seed_ref.qname, "identity": list(seed_identity)},
        "path": [step.to_dict() for step in origin.path],
    }


def compute_closure(
    *,
    tables: Iterable[TableRef],
    references: tuple[ReferenceSpec, ...],
    seeds: tuple[tuple[TableRef, tuple], ...],
    fetch_row: FetchRow,
    budget: int | None = None,
) -> Closure:
    """计算最小闭包。

    参数都应是 **已规范化** 的：references 按名字排序，seeds 按
    (表全限定名, 身份) 排序（引擎负责，保证配方排列不影响结果）。
    """
    selected: dict[TableRef, dict[tuple, dict[str, Any]]] = {t: {} for t in tables}
    origins: dict[tuple[TableRef, tuple], Origin] = {}
    refs_by_child: dict[TableRef, list[ReferenceSpec]] = {}
    for ref_spec in references:
        refs_by_child.setdefault(ref_spec.child, []).append(ref_spec)

    queue: deque[tuple[TableRef, tuple]] = deque()
    total = 0

    def _check_budget(next_ref: TableRef, next_identity: tuple) -> None:
        if budget is not None and total + 1 > budget:
            raise BudgetExceededError(
                f"记录预算不足：已确认必需 {total} 条（预算 {budget}），"
                f"还需要收入 {next_ref} 身份 {list(next_identity)}；"
                "裁取不截断关联，请提高预算后重试",
                budget=budget,
                confirmed=total,
                next_record={"table": next_ref.qname, "identity": list(next_identity)},
            )

    def _admit(
        ref: TableRef, identity: tuple, row: dict[str, Any], origin: Origin
    ) -> None:
        nonlocal total
        _check_budget(ref, identity)
        selected[ref][identity] = row
        origins[(ref, identity)] = origin
        queue.append((ref, identity))
        total += 1

    # 1) 全部起点先放入（重复起点只收一次）
    for table, identity in seeds:
        if identity in selected[table]:
            continue
        row = fetch_row(table, identity)
        if row is None:
            raise SeedNotFoundError(
                f"起始身份在源表中找不到: 表 {table} 身份 {list(identity)}",
                table=table.qname,
                identity=list(identity),
            )
        _admit(table, identity, row, Origin(seed=(table, identity), path=()))

    # 2) 广度优先：对每条新记录沿子表->父表引用补入父记录
    while queue:
        table, identity = queue.popleft()
        row = selected[table][identity]
        origin = origins[(table, identity)]
        for ref_spec in refs_by_child.get(table, []):
            values = tuple(row[col] for col in ref_spec.child_columns)
            if any(v is None for v in values):
                continue  # 复合值任意一项为空 -> 不需要父记录
            if values in selected[ref_spec.parent]:
                continue  # 已收（多路同身份/成环都在此收敛）
            parent_row = fetch_row(ref_spec.parent, values)
            step = PathStep(
                reference=ref_spec.name,
                child=table,
                child_identity=identity,
                parent=ref_spec.parent,
                parent_identity=values,
            )
            if parent_row is None:
                raise BrokenReferenceError(
                    f"记录 {table} 身份 {list(identity)} 经引用 "
                    f"'{ref_spec.name}' 指向的父记录不存在: "
                    f"{ref_spec.parent} 身份 {list(values)}",
                    reference=ref_spec.name,
                    child=table.qname,
                    child_identity=list(identity),
                    parent=ref_spec.parent.qname,
                    missing_parent_identity=list(values),
                    trail=_trail_payload(origin),
                )
            _admit(
                ref_spec.parent,
                values,
                parent_row,
                Origin(seed=origin.seed, path=origin.path + (step,)),
            )

    return Closure(selected=selected, origins=origins, total=total)
