"""视图查询的对象引用提取与拓扑排序。

只识别 *数据库对象*（表 / 视图）引用：

  * CTE 名（含嵌套 WITH）是查询内部的局部名字，遮蔽同名对象，不产生引用；
  * FROM/JOIN 里的表别名只是绑定名，不是另一个数据库对象；
  * 未限定的表名按"视图自身 schema 优先"解析（与 DuckDB 对视图内短名的
    解析规则一致），因此同 schema 的视图互相引用直接写短名即可；
  * 带 schema/catalog 限定的名字永远是对象引用。

拓扑排序用于决定发布执行顺序：被引用的一层先建。清单书写顺序不参与。
"""

from __future__ import annotations

import heapq

import sqlglot
from sqlglot import exp

DIALECT = "duckdb"

# 可以携带 WITH 子句的查询节点
_QUERY_NODES = (exp.Select, exp.Union, exp.Except, exp.Intersect)


class QueryShapeError(Exception):
    """查询文件不是可发布的单条 SELECT。"""


def parse_query(sql_text: str) -> exp.Expression:
    """解析并校验：恰好一条 SELECT（可含 CTE / UNION），不含 DDL/DML。"""
    try:
        statements = [
            s for s in sqlglot.parse(sql_text, read=DIALECT) if s is not None
        ]
    except sqlglot.errors.ParseError as ex:
        raise QueryShapeError(f"SQL 解析失败: {ex}") from ex
    if len(statements) != 1:
        raise QueryShapeError(
            f"查询文件必须恰好包含一条语句，实际 {len(statements)} 条"
        )
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.Union)):
        raise QueryShapeError(
            f"查询文件必须是一条 SELECT 查询，收到 {type(root).__name__}"
        )
    for node in root.walk():
        if isinstance(
            node,
            (
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Merge,
                exp.Create,
                exp.Alter,
                exp.Drop,
                exp.Command,
            ),
        ):
            raise QueryShapeError(
                f"查询文件不允许包含 {type(node).__name__} 语句"
            )
    return root


def normalize_ref(
    catalog: str,
    db: str,
    name: str,
    default_schema: str,
    current_database: str | None,
) -> str:
    """把 (catalog, schema, table) 三段规范化为小写身份串。

    本库对象统一为 ``schema.name``；指向其它 catalog 的三段名保留完整
    ``catalog.schema.name``，保证不会与本工具管理的名字相撞。
    """
    c = catalog.lower()
    d = db.lower()
    n = name.lower()
    if c:
        if current_database and c == current_database.lower():
            return f"{d}.{n}" if d else f"{default_schema}.{n}"
        return f"{c}.{d}.{n}" if d else f"{c}.{n}"
    if d:
        return f"{d}.{n}"
    return f"{default_schema}.{n}"


def extract_refs(
    root: exp.Expression,
    default_schema: str,
    current_database: str | None = None,
) -> set[str]:
    """提取查询引用的全部数据库对象（规范化身份集合）。"""
    refs: set[str] = set()
    _walk(root, frozenset(), refs, default_schema.lower(), current_database)
    return refs


def _walk(
    node: exp.Expression,
    visible: frozenset,
    refs: set[str],
    default_schema: str,
    current_database: str | None,
) -> None:
    if isinstance(node, exp.Table):
        name = node.name
        # 表函数 / 无名关系不是数据库对象引用
        if not name or isinstance(node.this, exp.Func):
            return
        # 未限定且命中可见 CTE：局部名字，遮蔽同名对象
        if not node.db and not node.catalog and name.lower() in visible:
            return
        refs.add(
            normalize_ref(
                node.catalog or "",
                node.db or "",
                name,
                default_schema,
                current_database,
            )
        )
        return

    if isinstance(node, _QUERY_NODES):
        local = set(visible)
        with_node = node.args.get("with_")
        if with_node is not None:
            for cte in with_node.expressions:
                # CTE 体只能看到外层及本 WITH 中先于自己定义的 CTE
                _walk(
                    cte.this,
                    frozenset(local),
                    refs,
                    default_schema,
                    current_database,
                )
                local.add(cte.alias.lower())
            visible = frozenset(local)

    for child in node.iter_expressions():
        if isinstance(child, (exp.With, exp.CTE)):
            continue  # WITH 已按作用域规则处理
        _walk(child, visible, refs, default_schema, current_database)


class CycleError(Exception):
    """候选视图之间存在循环引用；chain 为引用环（前者引用后者）。"""

    def __init__(self, chain: list[str]) -> None:
        super().__init__(" -> ".join(chain))
        self.chain = chain


def topo_order(names: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Kahn 拓扑排序：被引用者在前。并列时按名字字典序，保证确定性。

    ``deps[n]`` 是 n 引用（依赖）的名字集合（只含 names 内的成员）。
    存在环时抛出 :class:`CycleError`，携带一条具体环链。
    """
    indegree = {n: 0 for n in names}
    dependents: dict[str, set[str]] = {n: set() for n in names}
    for n in names:
        for d in deps.get(n, ()):
            if d in indegree:
                indegree[n] += 1
                dependents[d].add(n)
    ready = [n for n in names if indegree[n] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        n = heapq.heappop(ready)
        order.append(n)
        for m in dependents[n]:
            indegree[m] -= 1
            if indegree[m] == 0:
                heapq.heappush(ready, m)
    if len(order) != len(names):
        remaining = [n for n in names if n not in set(order)]
        raise CycleError(_find_cycle(remaining, deps))
    return order


def _find_cycle(nodes: list[str], deps: dict[str, set[str]]) -> list[str]:
    """在剩余子图中找一条环（DFS 回边），用于错误报告。"""
    nodeset = set(nodes)
    color = {n: 0 for n in nodes}  # 0=未访问 1=在栈上 2=已完成
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        color[n] = 1
        stack.append(n)
        for d in sorted(deps.get(n, ())) :
            if d not in nodeset:
                continue
            if color[d] == 1:
                return stack[stack.index(d):] + [d]
            if color[d] == 0:
                found = dfs(d)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return None

    for n in sorted(nodes):
        if color[n] == 0:
            found = dfs(n)
            if found:
                return found
    return sorted(nodes)  # 理论不可达；保底返回剩余集合
