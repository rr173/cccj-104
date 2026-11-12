"""缺陷复现 + 修复后断言（可直接运行：python3 -I repro/repro_row_existence.py）。

本脚本围绕"一条记录能否进入结果"这条链做端到端验证，分两部分：

A. 因果复现（DuckDB 真实执行）：同一组输入只改付款状态/清空订单表，
   观察数据库返回条数或行数确实改变——证明被漏报的依赖是真实因果；
B. 血缘断言（本仓库抽取器）：修复后的工具必须把这条因果解释成
   value/control 边，并保证无关字段不会凭空多出依赖（反"全列汇总"糊法）。

覆盖缺陷类别：
  1. 命名中间结果(CTE) / 匿名嵌套子查询里的筛选，外层 COUNT(*) 消费时丢失；
  2. 外层输出固定标记、或连接后只取对侧姓名时，内部筛选同样丢失；
  3. 多层嵌套后约束继续丢失；
  4. 内部只选常量的 EXISTS，删空基础表后影响无法追到行集；
  5. 标量子查询角色混淆：MAX 的 amount 被塞进 control、status 被算成 value；
  6. IN 子查询形态；
  7. 独立金额分支的条件泄漏到旁边固定标记（负向断言）。
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
from lineage.graph import downstream_impact, field_dependencies  # noqa: E402

STATUS = "shop.main.orders.status"
AMOUNT = "shop.main.orders.amount"
ORDERS_ROW_SET = "row_set:shop.main.orders"
CUST_NAME = "shop.main.customers.name"

# ---- 缺陷场景 SQL（均基于 catalog 中的 orders/status/customers/name） ----

CTE_COUNT = """
WITH paid_orders AS (
    SELECT order_id FROM orders WHERE status = 'PAID'
)
SELECT COUNT(*) AS n FROM paid_orders
"""

DERIVED_MARKER = """
SELECT 'flag' AS marker
FROM (SELECT 1 AS x FROM orders WHERE status = 'PAID') t
"""

JOIN_NAME = """
SELECT c.name AS customer_name
FROM customers c
JOIN (SELECT customer_id FROM orders WHERE status = 'PAID') o
  ON o.customer_id = c.customer_id
"""

THREE_LAYER_COUNT = """
SELECT COUNT(*) AS n FROM (
  SELECT * FROM (
    SELECT order_id FROM orders WHERE status = 'PAID'
  ) a
) b
"""

EXISTS_NONEMPTY = """
SELECT c.name AS customer_name FROM customers c
WHERE EXISTS (SELECT 1 FROM orders o)
"""

EXISTS_CORRELATED = """
SELECT c.name AS customer_name FROM customers c
WHERE EXISTS (
    SELECT 1 FROM orders o
    WHERE o.customer_id = c.customer_id AND o.status = 'PAID'
)
"""

SCALAR_MAX = """
SELECT c.name AS customer_name,
 (SELECT MAX(o.amount) FROM orders o
  WHERE o.customer_id = c.customer_id AND o.status = 'PAID') AS max_paid
FROM customers c
"""

IN_SUBQ = """
SELECT c.name AS customer_name FROM customers c
WHERE c.customer_id IN (
    SELECT customer_id FROM orders WHERE status = 'PAID'
)
"""

# 负向场景：金额分支条件只约束金额，固定标记不得产生任何依赖
BRANCH_LOCAL = """
SELECT CASE WHEN status = 'PAID' THEN amount ELSE 0 END AS paid_amount,
       'FIXED' AS marker
FROM orders
"""


def _refs(field, bucket: str) -> set[str]:
    return {s.ref for s in getattr(field, bucket)}


def _field(result, name: str):
    return next(f for f in result.fields if f.name == name)


# --------------------------------------------------------------------------
# A. DuckDB 因果复现
# --------------------------------------------------------------------------

def causal_part() -> list[str]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders(order_id INT, customer_id INT, "
        "status VARCHAR, amount DOUBLE)"
    )
    con.execute(
        "CREATE TABLE customers(customer_id INT, name VARCHAR)"
    )
    con.executemany(
        "INSERT INTO orders VALUES (?,?,?,?)",
        [
            (1, 1, "PAID", 10.0),
            (2, 1, "SHIPPED", 20.0),
            (3, 2, "PAID", 30.0),
        ],
    )
    con.executemany(
        "INSERT INTO customers VALUES (?,?)", [(1, "Alice"), (2, "Bob")]
    )
    evidence: list[str] = []

    # 因果 1：同一组输入只改付款状态，计数条数改变
    before = con.execute(CTE_COUNT).fetchone()[0]
    con.execute("UPDATE orders SET status = 'SHIPPED' WHERE order_id = 3")
    after = con.execute(CTE_COUNT).fetchone()[0]
    assert before != after, "DuckDB 中改变 status 应当改变 COUNT 条数"
    evidence.append(
        f"[因果1] 仅把一条订单 PAID->SHIPPED，COUNT 条数 {before} -> {after}"
    )

    # 因果 2：删空订单后，常量 EXISTS 不再保留任何客户行
    kept = con.execute(EXISTS_NONEMPTY).fetchall()
    con.execute("DELETE FROM orders")
    emptied = con.execute(EXISTS_NONEMPTY).fetchall()
    assert kept and not emptied, "删空 orders 后 EXISTS 结果应为空"
    evidence.append(
        f"[因果2] 删空 orders 前后，EXISTS 保留客户数 {len(kept)} -> {len(emptied)}"
    )
    return evidence


# --------------------------------------------------------------------------
# B. 修复后血缘断言
# --------------------------------------------------------------------------

def lineage_part(analyzer: LineageAnalyzer) -> list[str]:
    notes: list[str] = []

    # 1) CTE 计数：行集=value，status=control
    r = analyzer.analyze(CTE_COUNT)
    n = _field(r, "n")
    assert _refs(n, "value_sources") == {ORDERS_ROW_SET}
    assert STATUS in _refs(n, "control_sources")
    notes.append("[断言1] CTE+COUNT(*)：status 进入 control，行集仍为 value")

    # 2) 固定标记 + 匿名子查询：无 value，但 status 必须在 control
    r = analyzer.analyze(DERIVED_MARKER)
    marker = _field(r, "marker")
    assert _refs(marker, "value_sources") == set()
    assert STATUS in _refs(marker, "control_sources")
    notes.append("[断言2] 外层固定标记、不引用内层列：筛选约束未丢失")

    # 3) 连接后只取客户姓名
    r = analyzer.analyze(JOIN_NAME)
    name = _field(r, "customer_name")
    assert _refs(name, "value_sources") == {CUST_NAME}
    assert STATUS in _refs(name, "control_sources")
    notes.append("[断言3] 连接仅取 customers.name：订单付款筛选仍可追溯")

    # 4) 多层嵌套计数
    r = analyzer.analyze(THREE_LAYER_COUNT)
    n = _field(r, "n")
    assert _refs(n, "value_sources") == {ORDERS_ROW_SET}
    assert STATUS in _refs(n, "control_sources")
    notes.append("[断言4] 三层嵌套后约束继续上带，非单层补丁")

    # 5a) 常量 EXISTS：基础表行集 -> existence（删空表影响可追溯）
    r = analyzer.analyze(EXISTS_NONEMPTY)
    name = _field(r, "customer_name")
    assert ORDERS_ROW_SET in _refs(name, "existence_sources")
    assert ORDERS_ROW_SET not in _refs(name, "value_sources")
    assert ORDERS_ROW_SET not in _refs(name, "control_sources")
    notes.append("[断言5] 内部只选常量的 EXISTS：orders 行集进入 existence")

    # 5b) 相关 EXISTS：行集 -> existence，内层筛选列 -> control
    r = analyzer.analyze(EXISTS_CORRELATED)
    name = _field(r, "customer_name")
    assert ORDERS_ROW_SET in _refs(name, "existence_sources")
    assert STATUS in _refs(name, "control_sources")
    assert ORDERS_ROW_SET not in _refs(name, "control_sources")
    notes.append("[断言6] 相关 EXISTS：status=control，行集=existence")

    # 6) 标量子查询角色分桶：amount=value，status=control，空输入行集=existence
    r = analyzer.analyze(SCALAR_MAX)
    max_paid = _field(r, "max_paid")
    assert _refs(max_paid, "value_sources") == {AMOUNT}
    control = _refs(max_paid, "control_sources")
    assert STATUS in control
    assert AMOUNT not in control
    assert STATUS not in _refs(max_paid, "value_sources")
    assert ORDERS_ROW_SET in _refs(max_paid, "existence_sources")
    notes.append(
        "[断言7] 嵌套 MAX(amount)：amount=value、status=control、"
        "空输入行集=existence，三类原因不混淆"
    )

    # 7) IN 子查询：比较列 -> control，行集为空 -> existence
    r = analyzer.analyze(IN_SUBQ)
    name = _field(r, "customer_name")
    assert STATUS in _refs(name, "control_sources")
    assert ORDERS_ROW_SET in _refs(name, "existence_sources")
    assert ORDERS_ROW_SET not in _refs(name, "control_sources")
    assert _refs(name, "value_sources") == {CUST_NAME}
    notes.append("[断言8] IN 子查询：内部筛选=control、行集=existence")

    # 8) 负向：金额分支条件不得泄漏到旁边的固定标记
    r = analyzer.analyze(BRANCH_LOCAL)
    paid = _field(r, "paid_amount")
    marker = _field(r, "marker")
    assert STATUS in _refs(paid, "control_sources")
    assert _refs(marker, "value_sources") == set()
    assert _refs(marker, "control_sources") == set()
    notes.append("[断言9] 分支局部条件不污染兄弟字段（未用全列汇总糊法）")

    # 9) 两个操作入口（deps / impact）结论一致
    r = analyzer.analyze(SCALAR_MAX)
    deps = field_dependencies(r, "max_paid")
    assert deps["value_sources"] == [AMOUNT]
    assert STATUS in deps["control_sources"]
    assert ORDERS_ROW_SET in deps["existence_sources"]
    amount_impact = downstream_impact(r, AMOUNT)["impacted_fields"][0]
    status_impact = downstream_impact(r, STATUS)["impacted_fields"][0]
    row_set_impact = downstream_impact(r, ORDERS_ROW_SET)["impacted_fields"][0]
    assert amount_impact["impact_types"] == ["value"]
    assert status_impact["impact_types"] == ["control"]
    assert row_set_impact["impact_types"] == ["existence"]
    notes.append("[断言10] deps 与 impact 入口对 value/control/existence 结论一致")

    return notes


def main() -> int:
    print("=" * 68)
    print("A. DuckDB 因果复现（数据库真实行为）")
    print("=" * 68)
    for line in causal_part():
        print(line)

    print()
    print("=" * 68)
    print("B. 修复后血缘断言（抽取器解释同一条因果链）")
    print("=" * 68)
    analyzer = LineageAnalyzer(Catalog.load(ROOT / "catalog" / "catalog.json"))
    for line in lineage_part(analyzer):
        print(line)

    print()
    print("全部复现与断言通过：筛选约束沿 CTE/匿名子查询/连接/EXISTS/IN")
    print("独立上带，未选中任何内层列也不丢；value/control 角色无混淆、无泄漏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
