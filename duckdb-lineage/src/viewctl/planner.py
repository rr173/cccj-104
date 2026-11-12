"""发布计划：候选版本与库内已登记版本的差异 + 执行顺序 + 静态防线。

计划规则：

  * 管理集合以注册表为准（不是"库里碰巧存在的视图"）；
  * 新建 = 候选有、注册表没有；撤下 = 注册表有、候选没有；
  * 替换 = 两边都有但定义文本不同；一致则保留（keep，不产生操作）；
  * 执行顺序由视图间引用关系拓扑决定（被引用的一层先建），清单顺序无关；
  * 撤下放在最后，且被撤对象之间按旧定义的被依赖顺序逆序撤（先撤消费方）。

静态防线（全部在改动数据库之前完成）：

  * 候选循环引用（含自引用）；
  * 候选名字与库内非本工具管理的对象冲突（不收编、不覆盖别人的对象）；
  * 已登记对象在库内被换成了表等其它类型（类型冲突，需人工处理）；
  * 撤下仍被保留视图或外部视图引用的对象（附依赖链）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import err
from .manifest import Bundle
from .refs import CycleError, extract_refs, parse_query, topo_order
from .registry import DbState


@dataclass
class Op:
    seq: int
    action: str  # create | replace | drop
    name: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "action": self.action, "name": self.name}


@dataclass
class Plan:
    creates: list[str] = field(default_factory=list)
    replaces: list[str] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)
    keeps: list[str] = field(default_factory=list)
    order: list[Op] = field(default_factory=list)
    candidate_order: list[str] = field(default_factory=list)  # 全部候选的拓扑序

    @property
    def changed(self) -> set[str]:
        return set(self.creates) | set(self.replaces) | set(self.drops)

    def is_empty(self) -> bool:
        return not self.order

    def to_dict(self) -> dict:
        return {
            "create": self.creates,
            "replace": self.replaces,
            "drop": self.drops,
            "keep": self.keeps,
            "order": [op.to_dict() for op in self.order],
        }


def _chain_to_root(dependents_of: dict[str, set[str]], start: str) -> list[str]:
    """从 start 沿"被谁引用"向上走到根，返回 根 -> ... -> start 的链。"""
    path = [start]
    seen = {start}
    while True:
        ups = sorted(u for u in dependents_of.get(path[-1], ()) if u not in seen)
        if not ups:
            break
        path.append(ups[0])
        seen.add(ups[0])
    return list(reversed(path))


def build_plan(bundle: Bundle, state: DbState) -> tuple[Plan | None, list[dict]]:
    managed = state.managed
    candidate = bundle.assets

    # 候选内部依赖图（只统计指向候选名字的引用；外部对象不构成图边）
    deps = {
        name: {r for r in asset.refs if r in candidate}
        for name, asset in candidate.items()
    }
    try:
        candidate_order = topo_order(sorted(candidate), deps)
    except CycleError as ex:
        chain = ex.chain
        return None, [
            err(
                "cycle",
                "候选视图存在循环引用（-> 表示引用）: " + " -> ".join(chain),
                chain=chain,
            )
        ]

    errors: list[dict] = []

    # 名字冲突 / 类型冲突
    for name in sorted(candidate):
        if name in managed:
            if name in state.tables:
                errors.append(
                    err(
                        "object_type_conflict",
                        f"已登记对象 {name} 在库内当前是表而非视图；"
                        "为避免误删他人数据，请人工处理后再发布",
                        name=name,
                    )
                )
            continue
        if name in state.views or name in state.tables:
            kind = "视图" if name in state.views else "表"
            errors.append(
                err(
                    "name_collision",
                    f"候选视图 {name} 与库内已存在的非本工具管理{kind}冲突；"
                    "本工具不会收编或覆盖该对象，请改名或先人工移除",
                    name=name,
                )
            )
    for name in sorted(managed):
        if name not in candidate and name in state.tables:
            errors.append(
                err(
                    "object_type_conflict",
                    f"已登记对象 {name} 在库内当前是表而非视图；"
                    "为避免误删他人数据，请人工处理后再发布",
                    name=name,
                )
            )

    # 撤下保护：被撤对象不能被保留候选或外部视图引用
    drops = sorted(set(managed) - set(candidate))
    if drops:
        dependents_of: dict[str, set[str]] = {}
        for name, asset in candidate.items():
            for ref in asset.refs:
                dependents_of.setdefault(ref, set()).add(name)
        for dropped in drops:
            referenced_by: list[dict] = []
            for name in sorted(candidate):
                if dropped in candidate[name].refs:
                    chain = _chain_to_root(dependents_of, name) + [dropped]
                    referenced_by.append({"kind": "managed", "chain": chain})
            for ext_name, ext_refs in sorted(state.external_refs.items()):
                if ext_refs is None:
                    referenced_by.append(
                        {
                            "kind": "external_unparsed",
                            "chain": [ext_name, dropped],
                        }
                    )
                elif dropped in ext_refs:
                    referenced_by.append(
                        {"kind": "external", "chain": [ext_name, dropped]}
                    )
            if referenced_by:
                errors.append(
                    err(
                        "drop_blocked",
                        f"撤下 {dropped} 被阻止：仍被保留视图或外部视图引用"
                        "（chain 中前者引用后者）",
                        name=dropped,
                        referenced_by=referenced_by,
                    )
                )
    if errors:
        return None, errors

    # 动作分类
    plan = Plan(candidate_order=candidate_order)
    for name in candidate_order:
        if name not in managed:
            plan.creates.append(name)
        elif name not in state.views:
            # 登记在册但库内对象缺失（漂移）：按新建重装
            plan.creates.append(name)
        elif managed[name].sql_text != candidate[name].sql_text:
            plan.replaces.append(name)
        else:
            plan.keeps.append(name)
    plan.drops = drops

    # 撤下顺序：按旧定义的被依赖关系逆序（先撤消费方）。旧定义解析失败
    # 时退化为字典序——DuckDB 不强制视图依赖，顺序只影响整洁性。
    old_deps: dict[str, set[str]] = {}
    drop_set = set(drops)
    for name in drops:
        try:
            root = parse_query(managed[name].sql_text)
            refs = extract_refs(
                root,
                default_schema=name.split(".", 1)[0],
                current_database=state.database,
            )
            old_deps[name] = {r for r in refs if r in drop_set}
        except Exception:  # noqa: BLE001 - 注册表内旧定义损坏时的保底
            old_deps[name] = set()
    try:
        drop_order = list(reversed(topo_order(sorted(drops), old_deps)))
    except CycleError:  # 旧定义互相成环：字典序保底
        drop_order = sorted(drops)

    seq = 0
    action_of = {n: "create" for n in plan.creates}
    action_of.update({n: "replace" for n in plan.replaces})
    for name in candidate_order:  # 建/换严格按候选拓扑序（被引用层先就位）
        if name in action_of:
            seq += 1
            plan.order.append(Op(seq=seq, action=action_of[name], name=name))
    for name in drop_order:
        seq += 1
        plan.order.append(Op(seq=seq, action="drop", name=name))
    return plan, []
