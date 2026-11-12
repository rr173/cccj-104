"""复现 + 修复后断言：EXISTS 投影占位 / 标量常量 NULL / 空输入聚合。

可直接运行：``python3 -I repro/repro_exists_scalar_aggregate.py``

背景（线上变更评审遇到的两个互相矛盾的血缘图）：

1. ``EXISTS`` 只检查订单集合是否非空，旧工具却把子查询 ``SELECT`` 列表里的
   ``amount``/``status`` 列成"影响客户名单"的因素，审查者会去追查与结果无关
   的改动；
2. 另一条只返回常量的标量子查询，删掉最后一笔匹配订单后结果从 1 变成 NULL，
   旧工具却完全找不到订单来源，真正的行集变化被漏看。

本脚本用 DuckDB 建一组可重复的小数据，做两类真实执行实验：

A. 因果实验（数据库真实行为，确定性）：
   * 逐次删除 / 恢复匹配订单，记录每条查询的真实结果；
   * 单独修改只出现在 EXISTS 投影里的 ``status``/``amount``，记录结果是否改变；
   * 覆盖 NOT EXISTS、带匹配谓词的标量常量、空输入聚合（无分组 COUNT）。

B. 血缘断言（本仓库抽取器）：对每个真实变化，工具给出的来源必须与变化原因
   一致——值计算、匹配过滤、集合是否为空分别落在 value/control/existence，
   impact 类型也必须与之一致；并反向断言"占位投影/无关列不产生任何依赖"。

外加回归：此前已修复的派生关系筛选上带（COUNT(*) / 固定标记 / 多层嵌套 /
连接只取对侧列）与非法 SQL 拒绝（歧义列、递归 CTE、动态 SQL、解析错误）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

from lineage.analyzer import LineageAnalyzer  # noqa: E402
from lineage.catalog import Catalog  # noqa: E402
from lineage.graph import build_graph, downstream_impact, field_dependencies  # noqa: E402

# ---- 稳定身份 ----
STATUS = "shop.main.orders.status"
AMOUNT = "shop.main.orders.amount"
ORDER_CUSTOMER = "shop.main.orders.customer_id"
CUST_CUSTOMER = "shop.main.customers.customer_id"
CUST_NAME = "shop.main.customers.name"
ORDERS_ROW_SET = "row_set:shop.main.orders"

# 小数据：3 客户 / 3 订单；Bob 只有一笔 PAID，Alice 一笔 PAID 一笔 SHIPPED。
CUSTOMERS = [(1, "Alice"), (2, "Bob"), (3, "Carol")]
ORDERS_FULL = [
    (101, 1, "PAID", 200.0),
    (102, 1, "SHIPPED", 300.0),
    (103, 2, "PAID", 50.0),
]

# ---- 被测 SQL（与 sql/ 样例同义，保持独立便于确定性执行） ----

EXISTS_PROJECTS_COLUMNS = """
SELECT c.customer_id, c.name AS customer_name
FROM customers AS c
WHERE EXISTS (
    SELECT o.amount, o.status FROM orders AS o
    WHERE o.customer_id = c.customer_id
)
"""

NOT_EXISTS = """
SELECT c.customer_id, c.name AS customer_name
FROM customers AS c
WHERE NOT EXISTS (
    SELECT 1 FROM orders AS o
    WHERE o.customer_id = c.customer_id AND o.status = 'PAID'
)
"""

SCALAR_CONST_WITH_MATCH = """
SELECT c.customer_id,
       (SELECT 1 FROM orders AS o
        WHERE o.customer_id = c.customer_id AND o.status = 'PAID') AS has_paid
FROM customers AS c
"""

SCALAR_SUM_WITH_MATCH = """
SELECT c.customer_id,
       (SELECT SUM(o.amount) FROM orders AS o
        WHERE o.customer_id = c.customer_id AND o.status = 'PAID') AS paid_total
FROM customers AS c
"""

EMPTY_AGG_MARKER = """
SELECT t.paid_count AS n, 'fixed' AS marker
FROM (SELECT COUNT(*) AS paid_count FROM orders WHERE status = 'PAID') AS t
"""

COUNT_OVER_FILTERED_DERIVED = """
SELECT COUNT(*) AS n
FROM (SELECT order_id FROM orders WHERE status = 'PAID') AS t
"""

COUNT_OVER_FILTERED_CTE = """
WITH paid_orders AS (SELECT order_id FROM orders WHERE status = 'PAID')
SELECT COUNT(*) AS n FROM paid_orders
"""

MARKER_OVER_FILTERED_DERIVED = """
SELECT 'flag' AS marker
FROM (SELECT 1 AS x FROM orders WHERE status = 'PAID') AS t
"""

JOIN_OTHER_SIDE_ONLY = """
SELECT c.name AS customer_name
FROM customers AS c
JOIN (SELECT customer_id FROM orders WHERE status = 'PAID') AS o
  ON o.customer_id = c.customer_id
"""

THREE_LAYER_COUNT = """
SELECT COUNT(*) AS n FROM (
  SELECT * FROM (
    SELECT order_id FROM orders WHERE status = 'PAID'
  ) AS a
) AS b
"""

# 非法 / 范围外 SQL（回归拒绝行为）
RECURSIVE_CTE = """
WITH RECURSIVE walk(order_id, depth) AS (
    SELECT order_id, 0 FROM orders
    UNION ALL
    SELECT o.order_id, w.depth + 1 FROM orders o JOIN walk w ON o.order_id = w.order_id
)
SELECT * FROM walk
"""
DYNAMIC_SQL = "EXECUTE IMMEDIATE 'SELECT order_id FROM orders'"
PARSE_ERROR = "SELECT order_id FROM orders WHERE order_id ="
AMBIGUOUS = """
SELECT customer_id
FROM customers c JOIN orders o ON o.customer_id = c.customer_id
"""


def _refs(field, bucket: str) -> set[str]:
    return {s.ref for s in getattr(field, bucket)}


def _field(result, name: str):
    return next(f for f in result.fields if f.name == name)


# --------------------------------------------------------------------------
# 数据夹具：可重复建库 / 重置 / 逐次删除恢复
# --------------------------------------------------------------------------

def _new_db(orders) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE customers(customer_id INT, name VARCHAR)"
    )
    con.execute(
        "CREATE TABLE orders(order_id INT, customer_id INT, "
        "status VARCHAR, amount DOUBLE)"
    )
    con.executemany("INSERT INTO customers VALUES (?,?)", CUSTOMERS)
    con.executemany("INSERT INTO orders VALUES (?,?,?,?)", orders)
    return con


def _reset(con: duckdb.DuckDBPyConnection, orders) -> None:
    con.execute("DELETE FROM orders")
    if orders:
        con.executemany("INSERT INTO orders VALUES (?,?,?,?)", orders)
    con.execute("DELETE FROM customers")
    con.executemany("INSERT INTO customers VALUES (?,?)", CUSTOMERS)


def _rows_for_customer(con, sql: str) -> dict[int, object]:
    return {row[0]: row[1] for row in con.execute(sql).fetchall()}


# --------------------------------------------------------------------------
# A. 因果实验：真实执行，记录结果是否改变
# --------------------------------------------------------------------------

def causal_exists_projection() -> list[str]:
    """只改 EXISTS 投影里的 status/amount：结果必须不变（占位，不是筛选）。"""
    notes: list[str] = []
    sql = EXISTS_PROJECTS_COLUMNS

    con = _new_db(ORDERS_FULL)
    baseline = _rows_for_customer(con, sql)

    # 只把 Alice 的 SHIPPED 订单改成 PAID —— 只影响 status 值
    con.execute("UPDATE orders SET status='PAID' WHERE order_id=102")
    after_status = _rows_for_customer(con, sql)
    assert after_status == baseline, "只改 EXISTS 投影里的 status 不应改变保留行"
    notes.append(
        "[因果1] 只把投影列 status SHIPPED->PAID：保留客户名单不变 "
        f"{sorted(baseline)} —— 投影列是占位，不参与筛选"
    )

    # 只把金额翻倍 —— 只影响 amount 值
    con.execute("UPDATE orders SET amount = amount * 2")
    after_amount = _rows_for_customer(con, sql)
    assert after_amount == baseline, "只改 EXISTS 投影里的 amount 不应改变保留行"
    notes.append(
        "[因果2] 只把投影列 amount 全部翻倍：保留客户名单仍不变 "
        f"{sorted(baseline)}"
    )

    # 删除"匹配列" customer_id 所指的关系：删掉 Bob 的唯一订单
    con.execute("DELETE FROM orders WHERE order_id=103")
    after_match_delete = _rows_for_customer(con, sql)
    assert after_match_delete != baseline and 2 not in after_match_delete, (
        "删除 Bob 的唯一匹配订单后，Bob 应被 EXISTS 过滤掉"
    )
    notes.append(
        "[因果3] 删除 Bob 的匹配订单：名单 "
        f"{sorted(baseline)} -> {sorted(after_match_delete)} "
        "—— 真正的匹配条件变化才改变结果"
    )

    # 恢复 Bob 的订单：结果恢复（逐次删除/恢复）
    con.execute(
        "INSERT INTO orders VALUES (103, 2, 'PAID', 50.0)"
    )
    restored = _rows_for_customer(con, sql)
    assert restored == baseline, "恢复匹配订单后结果应回到原状"
    notes.append(
        "[因果4] 恢复 Bob 的订单：名单回到 "
        f"{sorted(restored)} —— 删除/恢复可重复"
    )
    return notes


def causal_not_exists() -> list[str]:
    """NOT EXISTS：删空 orders 让"没有匹配订单"恒真，结果翻转。"""
    notes: list[str] = []
    con = _new_db(ORDERS_FULL)
    before = _rows_for_customer(con, NOT_EXISTS)
    con.execute("DELETE FROM orders")
    empty = _rows_for_customer(con, NOT_EXISTS)
    assert before != empty, "删空 orders 后 NOT EXISTS 保留的客户集合应改变"
    notes.append(
        f"[因果5] NOT EXISTS：删空 orders 前后名单 {sorted(before)} -> {sorted(empty)}"
    )
    return notes


def causal_scalar_constant() -> list[str]:
    """标量常量：删掉最后一笔匹配订单，结果 1 -> NULL。"""
    notes: list[str] = []
    con = _new_db(ORDERS_FULL)
    before = _rows_for_customer(con, SCALAR_CONST_WITH_MATCH)
    assert before == {1: 1, 2: 1, 3: None}, f"初始应为 Alice/Bob=1, Carol=NULL，实际 {before}"

    con.execute("DELETE FROM orders WHERE order_id=103")  # Bob 的唯一匹配订单
    after = _rows_for_customer(con, SCALAR_CONST_WITH_MATCH)
    assert after[2] is None and before[2] == 1, "Bob 结果应从 1 变成 NULL"
    assert after[1] == 1, "Alice 仍有匹配订单，结果保持 1"
    notes.append(
        "[因果6] 标量常量：删除 Bob 唯一匹配订单，has_paid "
        f"1 -> NULL（行集变空，不是取值计算变化）；恢复后应回到 1"
    )

    con.execute("INSERT INTO orders VALUES (103, 2, 'PAID', 50.0)")
    restored = _rows_for_customer(con, SCALAR_CONST_WITH_MATCH)
    assert restored[2] == 1, "恢复订单后标量常量应回到 1"
    notes.append("[因果7] 恢复匹配订单：has_paid 回到 1")
    return notes


def causal_scalar_sum_null() -> list[str]:
    """标量 SUM：删掉最后匹配订单，结果变成 NULL；再改 amount 才改变取值。"""
    notes: list[str] = []
    con = _new_db(ORDERS_FULL)
    before = _rows_for_customer(con, SCALAR_SUM_WITH_MATCH)
    assert before[1] == 200.0 and before[2] == 50.0

    con.execute("DELETE FROM orders WHERE order_id=103")
    after_delete = _rows_for_customer(con, SCALAR_SUM_WITH_MATCH)
    assert after_delete[2] is None, "Bob 无匹配订单时 SUM 应为 NULL"
    notes.append(
        "[因果8] 标量 SUM：删除 Bob 匹配订单，paid_total 50.0 -> NULL"
    )

    con.execute("INSERT INTO orders VALUES (103, 2, 'PAID', 50.0)")
    con.execute("UPDATE orders SET amount=2000 WHERE order_id=103")
    changed = _rows_for_customer(con, SCALAR_SUM_WITH_MATCH)
    assert changed[2] == 2000.0 and before[2] == 50.0
    notes.append(
        "[因果9] 恢复订单并只改 amount：paid_total 50.0 -> 2000.0"
        "（取值来源变化，不是 NULL 变化）"
    )
    return notes


def causal_empty_aggregate() -> list[str]:
    """空输入聚合：无分组 COUNT 恒一行；删空 orders 只改变计数取值。"""
    notes: list[str] = []
    con = _new_db(ORDERS_FULL)
    before = con.execute(EMPTY_AGG_MARKER).fetchall()
    assert before == [(2, "fixed")], f"初始应为 [(2,'fixed')]，实际 {before}"

    con.execute("DELETE FROM orders")
    empty = con.execute(EMPTY_AGG_MARKER).fetchall()
    assert empty == [(0, "fixed")], f"空输入应为 [(0,'fixed')]，实际 {empty}"
    notes.append(
        "[因果10] 无分组 COUNT：删空 orders 后 n 2 -> 0 且仍返回一行，"
        "固定标记 marker 保持不变（内层筛选不作用于外层行选择）"
    )

    con.execute("INSERT INTO orders VALUES (101, 1, 'PAID', 200.0)")
    partial = con.execute(EMPTY_AGG_MARKER).fetchall()
    assert partial == [(1, "fixed")], f"恢复一笔 PAID 后应为 [(1,'fixed')]，实际 {partial}"
    notes.append("[因果11] 恢复一笔 PAID：n 回到 1，仍只有一行")
    return notes


def causal_part() -> list[str]:
    evidence: list[str] = []
    evidence += causal_exists_projection()
    evidence += causal_not_exists()
    evidence += causal_scalar_constant()
    evidence += causal_scalar_sum_null()
    evidence += causal_empty_aggregate()
    return evidence


# --------------------------------------------------------------------------
# B. 血缘断言：工具必须解释上面每一条真实因果
# --------------------------------------------------------------------------

def lineage_exists_projection(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    r = a.analyze(EXISTS_PROJECTS_COLUMNS)
    assert r.status == "ok", r.errors
    name = _field(r, "customer_name")
    # 占位投影：amount/status 不得出现在任何桶
    for bucket in ("value_sources", "control_sources", "existence_sources"):
        assert AMOUNT not in _refs(name, bucket), f"amount 不应出现在 {bucket}"
        assert STATUS not in _refs(name, bucket), f"status 不应出现在 {bucket}"
    # 匹配条件 -> control；行集是否为空 -> existence
    control = _refs(name, "control_sources")
    assert ORDER_CUSTOMER in control and CUST_CUSTOMER in control
    assert ORDERS_ROW_SET in _refs(name, "existence_sources")
    assert ORDERS_ROW_SET not in control
    # value 只有真正用于输出的客户列
    assert _refs(name, "value_sources") == {CUST_NAME}
    notes.append(
        "[断言1] EXISTS 投影占位：amount/status 三桶皆无；匹配列=control，行集=existence"
    )

    # impact 入口：投影列无任何影响；匹配列=control；行集=existence
    assert downstream_impact(r, AMOUNT)["impacted_fields"] == []
    assert downstream_impact(r, STATUS)["impacted_fields"] == []
    impact_row_set = {
        i["field"]: i["impact_types"]
        for i in downstream_impact(r, ORDERS_ROW_SET)["impacted_fields"]
    }
    assert all(
        types == ["existence"] for types in impact_row_set.values()
    )
    assert "<top_query>.customer_name" in impact_row_set
    impact_match = downstream_impact(r, ORDER_CUSTOMER)["impacted_fields"]
    assert all("control" in i["impact_types"] for i in impact_match)
    notes.append(
        "[断言2] impact：改投影列无下游；改匹配列=control；删空行集=existence"
    )

    # 图边：不得存在 amount/status 指向该字段的任何边
    graph = build_graph(r)
    for edge in graph["edges"]:
        if edge["to"] == "<top_query>.customer_name":
            assert edge["from"] not in (AMOUNT, STATUS), edge
    notes.append("[断言3] 图边：无 amount/status 指向 customer_name 的边")
    return notes


def lineage_not_exists(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    r = a.analyze(NOT_EXISTS)
    name = _field(r, "customer_name")
    control = _refs(name, "control_sources")
    assert STATUS in control and ORDER_CUSTOMER in control
    assert ORDERS_ROW_SET in _refs(name, "existence_sources")
    assert ORDERS_ROW_SET not in control
    notes.append("[断言4] NOT EXISTS：匹配列=control，行集=existence（两侧对称）")
    return notes


def lineage_scalar_constant(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    r = a.analyze(SCALAR_CONST_WITH_MATCH)
    flag = _field(r, "has_paid")
    # 常量本身不引入基础列 value
    assert _refs(flag, "value_sources") == set()
    # 匹配条件保留
    control = _refs(flag, "control_sources")
    assert STATUS in control and ORDER_CUSTOMER in control
    # 输入集合变空 -> NULL：行集必须可追溯
    existence = _refs(flag, "existence_sources")
    assert ORDERS_ROW_SET in existence
    assert ORDERS_ROW_SET not in control

    deps = field_dependencies(r, "has_paid")
    assert deps["existence_sources"] == [ORDERS_ROW_SET]
    row_set_impact = downstream_impact(r, ORDERS_ROW_SET)["impacted_fields"]
    assert [i["impact_types"] for i in row_set_impact] == [["existence"]]
    status_impact = downstream_impact(r, STATUS)["impacted_fields"]
    assert status_impact[0]["impact_types"] == ["control"]
    notes.append(
        "[断言5] 标量常量：匹配列=control，行集=existence（1->NULL 可追溯），"
        "常量无 value；deps/impact 一致"
    )
    return notes


def lineage_scalar_sum(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    r = a.analyze(SCALAR_SUM_WITH_MATCH)
    total = _field(r, "paid_total")
    assert _refs(total, "value_sources") == {AMOUNT}
    assert AMOUNT not in _refs(total, "control_sources")
    assert STATUS in _refs(total, "control_sources")
    # SUM 空输入变 NULL：行集也在 existence；与取值来源 amount 分开
    assert ORDERS_ROW_SET in _refs(total, "existence_sources")
    notes.append(
        "[断言6] 标量 SUM：amount=value、status=control、行集=existence，三类原因分开"
    )
    return notes


def lineage_empty_aggregate(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    r = a.analyze(EMPTY_AGG_MARKER)
    n = _field(r, "n")
    marker = _field(r, "marker")
    # 行数变化是取值来源(value)；status 改变计数取值，属匹配原因(control)
    assert _refs(n, "value_sources") == {ORDERS_ROW_SET}
    assert STATUS in _refs(n, "control_sources")
    # 关键：内层筛选不得扩散成外层固定标记的行选择来源
    assert _refs(marker, "value_sources") == set()
    assert _refs(marker, "control_sources") == set()
    assert _refs(marker, "existence_sources") == set()
    notes.append(
        "[断言7] 空输入无分组 COUNT：n 的行集=value、status=control；"
        "外层 marker 完全不受内层筛选影响"
    )
    return notes


def lineage_regressions(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    # 派生关系筛选上带：COUNT(*) 不引用内层列也不丢 status
    for sql, label in (
        (COUNT_OVER_FILTERED_DERIVED, "匿名派生"),
        (COUNT_OVER_FILTERED_CTE, "CTE"),
    ):
        r = a.analyze(sql)
        n = _field(r, "n")
        assert _refs(n, "value_sources") == {ORDERS_ROW_SET}
        assert STATUS in _refs(n, "control_sources")
        assert STATUS not in _refs(n, "value_sources")
    notes.append("[回归1] COUNT(*) 消费筛选派生关系：status 仍为 control，行集仍为 value")

    r = a.analyze(MARKER_OVER_FILTERED_DERIVED)
    marker = _field(r, "marker")
    assert _refs(marker, "value_sources") == set()
    assert STATUS in _refs(marker, "control_sources")
    notes.append("[回归2] 固定标记 + 匿名派生：内层筛选仍上带为 control")

    r = a.analyze(JOIN_OTHER_SIDE_ONLY)
    name = _field(r, "customer_name")
    assert _refs(name, "value_sources") == {CUST_NAME}
    assert STATUS in _refs(name, "control_sources")
    notes.append("[回归3] 连接仅取对侧姓名：订单侧筛选仍可追溯")

    r = a.analyze(THREE_LAYER_COUNT)
    n = _field(r, "n")
    assert _refs(n, "value_sources") == {ORDERS_ROW_SET}
    assert STATUS in _refs(n, "control_sources")
    notes.append("[回归4] 三层嵌套：筛选约束继续上带，非单层补丁")

    # 负数回归：分支局部条件不得泄漏到兄弟固定标记
    branch = """
        SELECT CASE WHEN status='PAID' THEN amount ELSE 0 END AS paid_amount,
               'FIXED' AS marker
        FROM orders
    """
    br = a.analyze(branch)
    assert STATUS in _refs(_field(br, "paid_amount"), "control_sources")
    assert _refs(_field(br, "marker"), "control_sources") == set()
    notes.append("[回归5] 负向：CASE 局部条件不污染兄弟固定标记")
    return notes


def lineage_rejections(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    r = a.analyze(RECURSIVE_CTE)
    assert r.status == "unsupported" and r.fields == []
    assert r.errors[0]["code"] == "unsupported_syntax"
    notes.append("[回归6] 递归 CTE 仍明确拒绝，不产出半截血缘")

    r = a.analyze(DYNAMIC_SQL)
    assert r.status == "unsupported" and r.fields == []

    r = a.analyze(PARSE_ERROR)
    assert r.status == "unsupported" and r.errors[0]["code"] == "parse_error"
    notes.append("[回归7] 动态 SQL / 解析错误仍拒绝并给出结构化错误")

    r = a.analyze(AMBIGUOUS)
    assert r.status == "error" and r.errors[0]["code"] == "ambiguous_column"
    assert set(r.errors[0]["candidates"]) == {"c.customer_id", "o.customer_id"}
    notes.append("[回归8] 非法歧义列仍诊断到候选绑定")
    return notes


def lineage_part(a: LineageAnalyzer) -> list[str]:
    notes: list[str] = []
    notes += lineage_exists_projection(a)
    notes += lineage_not_exists(a)
    notes += lineage_scalar_constant(a)
    notes += lineage_scalar_sum(a)
    notes += lineage_empty_aggregate(a)
    notes += lineage_regressions(a)
    notes += lineage_rejections(a)
    return notes


def main() -> int:
    print("=" * 72)
    print("A. DuckDB 因果复现（真实执行：逐次删除/恢复、只改投影列）")
    print("=" * 72)
    for line in causal_part():
        print(line)

    print()
    print("=" * 72)
    print("B. 修复后血缘/impact 断言（工具解释同一条因果链）")
    print("=" * 72)
    analyzer = LineageAnalyzer(Catalog.load(ROOT / "catalog" / "catalog.json"))
    for line in lineage_part(analyzer):
        print(line)

    print()
    print("全部因果实验与断言通过：")
    print("- EXISTS/NOT EXISTS 的 SELECT 占位列不产生任何依赖，匹配列=control，行集=existence；")
    print("- 标量子查询空输入导致 NULL 的变化可追到行集，具体匹配条件保留；")
    print("- 空输入无分组 COUNT 的行筛选只影响取值，不扩散为外层行选择；")
    print("- 派生查询筛选上带与非法 SQL 拒绝回归全部保持。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
