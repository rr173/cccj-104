"""pytest 单元测试：聚焦核心语义，不依赖文件系统布局之外的服务。

运行：python3 -I -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

from lineage.analyzer import LineageAnalyzer  # noqa: E402
from lineage.catalog import Catalog  # noqa: E402
from lineage.graph import build_graph, downstream_impact, field_dependencies  # noqa: E402
from lineage.model import is_row_set_marker  # noqa: E402


@pytest.fixture(scope="module")
def analyzer() -> LineageAnalyzer:
    return LineageAnalyzer(Catalog.load(ROOT / "catalog" / "catalog.json"))


def _field(result, name: str):
    return next(f for f in result.fields if f.name == name)


def _refs(field, bucket: str) -> set[str]:
    return {s.ref for s in getattr(field, bucket)}


def test_cte_shadows_base_table(analyzer):
    sql = (ROOT / "sql" / "01_cte_shadow.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    assert result.status == "ok"
    amount = _field(result, "amount")
    # 外层 CTE 遮蔽后仍重定基到基础身份，而非 CTE 名拼接
    assert _refs(amount, "value_sources") == {"shop.main.orders.amount"}
    # CTE 内部过滤穿透为 control
    assert "shop.main.orders.status" in _refs(amount, "control_sources")


def test_two_layer_alias_no_fake_edge(analyzer):
    sql = (ROOT / "sql" / "02_two_layer_alias.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    oid = _field(result, "oid")
    assert _refs(oid, "value_sources") == {
        "shop.main.order_items.order_id"
    }
    # 任何来源都不得以派生表别名 d. 作为身份
    assert all(not s.ref.startswith("d.") for s in oid.value_sources)


def test_star_expansion_count(analyzer):
    sql = (ROOT / "sql" / "03_star.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    # 裸 * 10 列 + c.* 5 列
    assert len(result.fields) == 15


def test_correlated_exists_only_control(analyzer):
    sql = (ROOT / "sql" / "04_correlated_exists.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    for field in result.fields:
        value = _refs(field, "value_sources")
        assert all("order_items" not in r for r in value)
        control = _refs(field, "control_sources")
        assert "shop.main.order_items.qty" in control
        assert "shop.main.order_items.order_id" in control
        # EXISTS 内部 FROM 的行集是"行集是否为空"原因，单列 existence，
        # 不并进匹配条件 control（两者是不同变化原因）。
        assert "row_set:shop.main.order_items" in _refs(field, "existence_sources")
        assert "row_set:shop.main.order_items" not in control
        assert "row_set:shop.main.order_items" not in value


def test_union_all_positional_merge(analyzer):
    sql = (ROOT / "sql" / "05_union_all.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    id_field = _field(result, "id")  # 输出名取左分支
    assert _refs(id_field, "value_sources") == {
        "shop.main.orders.order_id",
        "shop.main.order_items.item_id",
    }


def test_case_and_window_buckets(analyzer):
    sql = (ROOT / "sql" / "06_case_window.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    label = _field(result, "customer_label")
    assert _refs(label, "value_sources") == {"shop.main.customers.name"}
    assert "shop.main.customers.tier" in _refs(label, "control_sources")
    assert "shop.main.customers.tier" not in _refs(label, "value_sources")

    rank = _field(result, "city_rank")
    rank_value = _refs(rank, "value_sources")
    assert all(is_row_set_marker(r) for r in rank_value)
    assert "shop.main.customers.city" in _refs(rank, "control_sources")


def test_count_star_uses_row_set_not_columns(analyzer):
    sql = (ROOT / "sql" / "06_case_window.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    counter = _field(result, "city_row_counter")
    value = _refs(counter, "value_sources")
    assert value == {
        "row_set:shop.main.customers",
        "row_set:shop.main.orders",
    }
    # 不得伪造任何具体列依赖
    assert all(is_row_set_marker(r) for r in value)


def test_ambiguous_column_diagnosed(analyzer):
    sql = (ROOT / "sql" / "07_ambiguous.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    assert result.status == "error"
    err = result.errors[0]
    assert err["code"] == "ambiguous_column"
    assert set(err["candidates"]) == {"c.customer_id", "o.customer_id"}
    assert err["location"]["line"] == 5


def test_recursive_cte_unsupported(analyzer):
    sql = (ROOT / "sql" / "08a_recursive_cte.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    assert result.status == "unsupported"
    assert result.fields == []
    assert result.errors[0]["code"] == "unsupported_syntax"


def test_dynamic_sql_unsupported(analyzer):
    sql = (ROOT / "sql" / "08b_dynamic_sql.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    assert result.status == "unsupported"
    assert result.fields == []


def test_parse_error_has_location(analyzer):
    sql = (ROOT / "sql" / "08c_parse_error.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    assert result.status == "unsupported"
    err = result.errors[0]
    assert err["code"] == "parse_error"
    assert err["location"]["line"] == 5
    assert err["location"]["col"] == 14


def test_filter_only_column_distinguishes_buckets(analyzer):
    sql = (ROOT / "sql" / "09_filter_only_column.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    name = _field(result, "customer_name")
    assert _refs(name, "value_sources") == {"shop.main.customers.name"}
    control = _refs(name, "control_sources")
    assert "shop.main.customers.city" in control
    assert "shop.main.customers.tier" in control
    assert "shop.main.customers.city" not in _refs(name, "value_sources")


def test_graph_and_queries(analyzer):
    sql = (ROOT / "sql" / "09_filter_only_column.sql").read_text(encoding="utf-8")
    result = analyzer.analyze(sql)
    graph = build_graph(result)
    edge_pairs = {(e["from"], e["to"], e["type"]) for e in graph["edges"]}
    assert (
        "shop.main.customers.name",
        "<top_query>.customer_name",
        "value",
    ) in edge_pairs
    assert (
        "shop.main.customers.city",
        "<top_query>.customer_name",
        "control",
    ) in edge_pairs

    deps = field_dependencies(result, "customer_name")
    assert deps["value_sources"] == ["shop.main.customers.name"]

    impact = downstream_impact(result, "shop.main.customers.city")
    fields = {i["field"] for i in impact["impacted_fields"]}
    assert "<top_query>.customer_name" in fields
    for item in impact["impacted_fields"]:
        assert item["impact_types"] == ["control"]


def test_unknown_relation_rejected(analyzer):
    result = analyzer.analyze("SELECT x FROM nonexistent_table")
    assert result.status == "error"
    assert result.errors[0]["code"] == "unknown_relation"


def test_union_distinct_unsupported(analyzer):
    result = analyzer.analyze(
        "SELECT customer_id FROM customers UNION SELECT customer_id FROM orders"
    )
    assert result.status == "unsupported"


def test_lateral_unsupported(analyzer):
    result = analyzer.analyze(
        "SELECT * FROM customers c, LATERAL (SELECT 1) z"
    )
    assert result.status == "unsupported"
    assert result.errors[0]["code"] == "unsupported_syntax"


def test_multi_statement_unsupported(analyzer):
    result = analyzer.analyze("SELECT 1; SELECT 2")
    assert result.status == "unsupported"


# ---------------------------------------------------------------------------
# 回归：记录能否进入结果（行存在性）链路的血缘
#
# 旧实现把派生关系内部的筛选约束绑死在"被外层选中的列"上：外层不引用任何
# 内层列（COUNT(*)/固定标记/只取连接侧姓名）时，WHERE 的 control 依赖丢失。
# 以下用例沿 CTE -> 匿名子查询 -> 连接/计数消费 的链路逐层锁定。
# ---------------------------------------------------------------------------


def test_count_over_filtered_derived_keeps_filter_control(analyzer):
    # 改 status 会改变 COUNT(*) 的条数；status 必须作为 control 可追溯，
    # 无论计数输入来自匿名子查询还是命名 CTE。
    sql = """
        SELECT COUNT(*) AS n
        FROM (SELECT order_id FROM orders WHERE status = 'PAID') AS t
    """
    result = analyzer.analyze(sql)
    n = _field(result, "n")
    assert _refs(n, "value_sources") == {"row_set:shop.main.orders"}
    assert "shop.main.orders.status" in _refs(n, "control_sources")

    cte_sql = """
        WITH paid_orders AS (
            SELECT order_id FROM orders WHERE status = 'PAID'
        )
        SELECT COUNT(*) AS n FROM paid_orders
    """
    cte_result = analyzer.analyze(cte_sql)
    cte_n = _field(cte_result, "n")
    assert "shop.main.orders.status" in _refs(cte_n, "control_sources")
    assert _refs(cte_n, "value_sources") == {"row_set:shop.main.orders"}


def test_constant_marker_over_filtered_derived_keeps_filter(analyzer):
    # 外层只输出固定标记、不取内层任何一列，内部筛选仍不能丢。
    sql = """
        SELECT 'flag' AS marker
        FROM (SELECT 1 AS x FROM orders WHERE status = 'PAID') AS t
    """
    result = analyzer.analyze(sql)
    marker = _field(result, "marker")
    assert _refs(marker, "value_sources") == set()
    assert "shop.main.orders.status" in _refs(marker, "control_sources")


def test_join_consuming_only_other_side_keeps_filter(analyzer):
    # 连接后只取 customers.name，派生订单表的付款筛选仍决定哪些客户行保留。
    sql = """
        SELECT c.name AS customer_name
        FROM customers AS c
        JOIN (
            SELECT customer_id FROM orders WHERE status = 'PAID'
        ) AS o ON o.customer_id = c.customer_id
    """
    result = analyzer.analyze(sql)
    name = _field(result, "customer_name")
    assert _refs(name, "value_sources") == {"shop.main.customers.name"}
    assert "shop.main.orders.status" in _refs(name, "control_sources")

    # 即便外层完全不引用派生表任何列（恒真连接），约束也必须保留
    sql_no_col = """
        SELECT c.name AS customer_name
        FROM customers AS c
        JOIN (
            SELECT 1 AS x FROM orders WHERE status = 'PAID'
        ) AS o ON 1 = 1
    """
    no_col = analyzer.analyze(sql_no_col)
    name2 = _field(no_col, "customer_name")
    assert "shop.main.orders.status" in _refs(name2, "control_sources")


def test_row_filter_survives_multi_layer_nesting(analyzer):
    # 三层嵌套：最内层 WHERE status，最外层只输出常量/做 COUNT(*)。
    marker_sql = """
        SELECT 'flag' AS marker FROM (
            SELECT * FROM (
                SELECT order_id FROM orders WHERE status = 'PAID'
            ) AS a
        ) AS b
    """
    marker = _field(analyzer.analyze(marker_sql), "marker")
    assert "shop.main.orders.status" in _refs(marker, "control_sources")

    count_sql = """
        SELECT COUNT(*) AS n FROM (
            SELECT * FROM (
                SELECT order_id FROM orders WHERE status = 'PAID'
            ) AS a
        ) AS b
    """
    n = _field(analyzer.analyze(count_sql), "n")
    assert "shop.main.orders.status" in _refs(n, "control_sources")
    assert _refs(n, "value_sources") == {"row_set:shop.main.orders"}


def test_exists_constant_select_traces_base_row_set(analyzer):
    # 内部只选常量、仅凭 orders 是否非空决定客户记录是否保留：
    # 删空 orders 后结果消失，因此 orders 行集必须可追溯到 existence。
    sql = """
        SELECT c.name AS customer_name
        FROM customers AS c
        WHERE EXISTS (SELECT 1 FROM orders AS o)
    """
    result = analyzer.analyze(sql)
    name = _field(result, "customer_name")
    existence = _refs(name, "existence_sources")
    assert "row_set:shop.main.orders" in existence
    assert "row_set:shop.main.orders" not in _refs(name, "value_sources")
    assert "row_set:shop.main.orders" not in _refs(name, "control_sources")

    # 相关 EXISTS：内层匹配列进 control，行集进 existence
    correlated = """
        SELECT c.name AS customer_name
        FROM customers AS c
        WHERE EXISTS (
            SELECT 1 FROM orders AS o
            WHERE o.customer_id = c.customer_id AND o.status = 'PAID'
        )
    """
    corr = _field(analyzer.analyze(correlated), "customer_name")
    assert "row_set:shop.main.orders" in _refs(corr, "existence_sources")
    assert "shop.main.orders.status" in _refs(corr, "control_sources")


def test_scalar_subquery_preserves_value_control_roles(analyzer):
    # 嵌套计算按付款状态取最大金额：amount 是取值来源(value)，
    # status/customer_id 只做筛选(control)，角色不得互换或混桶。
    sql = """
        SELECT (
            SELECT MAX(o.amount)
            FROM orders AS o
            WHERE o.customer_id = c.customer_id AND o.status = 'PAID'
        ) AS max_paid
        FROM customers AS c
    """
    result = analyzer.analyze(sql)
    max_paid = _field(result, "max_paid")
    assert _refs(max_paid, "value_sources") == {"shop.main.orders.amount"}
    control = _refs(max_paid, "control_sources")
    assert "shop.main.orders.status" in control
    assert "shop.main.orders.customer_id" in control
    # 旧缺陷：amount 被塞进筛选来源、status 被算进取值来源
    assert "shop.main.orders.amount" not in control
    assert "shop.main.orders.status" not in _refs(max_paid, "value_sources")


def test_branch_local_condition_does_not_leak_to_sibling_field(analyzer):
    # 反"汇总所有列来源"糊法：某个金额分支的条件只约束该金额，
    # 不能变成旁边固定标记的依赖。
    sql = """
        SELECT
            CASE WHEN status = 'PAID' THEN amount ELSE 0 END AS paid_amount,
            'FIXED' AS marker
        FROM orders
    """
    result = analyzer.analyze(sql)
    paid_amount = _field(result, "paid_amount")
    assert "shop.main.orders.status" in _refs(paid_amount, "control_sources")
    marker = _field(result, "marker")
    assert _refs(marker, "value_sources") == set()
    assert _refs(marker, "control_sources") == set()

    # 关系级筛选仍应约束同层全部字段（与表达式内 CASE 条件的局部性区分）
    relation_sql = """
        SELECT 'FIXED' AS marker
        FROM orders
        WHERE status = 'PAID'
    """
    rel_marker = _field(analyzer.analyze(relation_sql), "marker")
    assert "shop.main.orders.status" in _refs(rel_marker, "control_sources")


def test_in_subquery_filter_and_row_set_are_control(analyzer):
    sql = """
        SELECT c.name AS customer_name
        FROM customers AS c
        WHERE c.customer_id IN (
            SELECT customer_id FROM orders WHERE status = 'PAID'
        )
    """
    result = analyzer.analyze(sql)
    name = _field(result, "customer_name")
    control = _refs(name, "control_sources")
    assert "shop.main.orders.status" in control
    # IN 读取投影列做成员比较 -> 投影列属于匹配原因(control)
    assert "shop.main.orders.customer_id" in control
    # 行集是否为空属于 existence，不与匹配条件混桶
    assert "row_set:shop.main.orders" in _refs(name, "existence_sources")
    assert "row_set:shop.main.orders" not in control
    assert _refs(name, "value_sources") == {"shop.main.customers.name"}


def test_deps_and_impact_entries_give_consistent_roles(analyzer):
    # deps（查询依赖）与 impact（展示影响类型）两个入口对同一来源必须一致。
    sql = """
        SELECT (
            SELECT MAX(o.amount)
            FROM orders AS o
            WHERE o.status = 'PAID'
        ) AS max_paid
        FROM customers AS c
    """
    result = analyzer.analyze(sql)
    deps = field_dependencies(result, "max_paid")
    assert deps["value_sources"] == ["shop.main.orders.amount"]
    assert "shop.main.orders.status" in deps["control_sources"]

    amount_impact = downstream_impact(result, "shop.main.orders.amount")
    assert amount_impact["impacted_fields"][0]["impact_types"] == ["value"]
    status_impact = downstream_impact(result, "shop.main.orders.status")
    assert status_impact["impacted_fields"][0]["impact_types"] == ["control"]

    # 计数场景：行集=value，筛选列=control，impact 入口同样可解释
    count_sql = """
        SELECT COUNT(*) AS n
        FROM (SELECT order_id FROM orders WHERE status = 'PAID') AS t
    """
    count_result = analyzer.analyze(count_sql)
    status_on_count = downstream_impact(
        count_result, "shop.main.orders.status"
    )
    assert status_on_count["impacted_fields"][0]["impact_types"] == ["control"]
    row_set_impact = downstream_impact(
        count_result, "row_set:shop.main.orders"
    )
    assert row_set_impact["impacted_fields"][0]["impact_types"] == ["value"]


# ---------------------------------------------------------------------------
# 回归：EXISTS 投影占位 / 标量 NULL / 空输入聚合（三类原因分开）
#
# 旧模型把 EXISTS 内部所有来源一股脑并进 control：只出现在 SELECT 列表里的
# 列被误报为筛选条件；标量子查询变 NULL 的行集原因、以及无分组聚合的行集
# 语义又都无处可放。以下用例锁定 value / control / existence 的严格分工。
# ---------------------------------------------------------------------------


def test_exists_projection_columns_are_placeholders(analyzer):
    # EXISTS 的 SELECT 列表只是占位：改 amount/status 不改变保留行。
    r = analyzer.analyze(
        (ROOT / "sql" / "15_exists_projection_placeholder.sql").read_text(
            encoding="utf-8"
        )
    )
    name = _field(r, "customer_name")
    for bucket in ("value_sources", "control_sources", "existence_sources"):
        assert "shop.main.orders.amount" not in _refs(name, bucket)
        assert "shop.main.orders.status" not in _refs(name, bucket)
    assert "shop.main.orders.customer_id" in _refs(name, "control_sources")
    assert "row_set:shop.main.orders" in _refs(name, "existence_sources")
    assert "row_set:shop.main.orders" not in _refs(name, "control_sources")


def test_not_exists_matches_existence_and_filter(analyzer):
    r = analyzer.analyze(
        (ROOT / "sql" / "16_not_exists.sql").read_text(encoding="utf-8")
    )
    name = _field(r, "customer_name")
    assert "shop.main.orders.status" in _refs(name, "control_sources")
    assert "row_set:shop.main.orders" in _refs(name, "existence_sources")
    assert "row_set:shop.main.orders" not in _refs(name, "control_sources")


def test_scalar_constant_with_predicate_traces_null_to_row_set(analyzer):
    r = analyzer.analyze(
        (ROOT / "sql" / "17_scalar_constant_with_predicate.sql").read_text(
            encoding="utf-8"
        )
    )
    flag = _field(r, "has_paid_order")
    assert _refs(flag, "value_sources") == set()
    assert "shop.main.orders.status" in _refs(flag, "control_sources")
    assert "shop.main.orders.customer_id" in _refs(flag, "control_sources")
    assert _refs(flag, "existence_sources") == {"row_set:shop.main.orders"}

    deps = field_dependencies(r, "has_paid_order")
    assert deps["existence_sources"] == ["row_set:shop.main.orders"]
    impact = downstream_impact(r, "row_set:shop.main.orders")["impacted_fields"]
    assert impact[0]["impact_types"] == ["existence"]


def test_empty_input_aggregate_filter_does_not_leak_to_outer_field(analyzer):
    # 无分组 COUNT 恒一行：内层 WHERE 只改变取值，不得扩散成外层固定标记的
    # 行选择来源。
    r = analyzer.analyze(
        (ROOT / "sql" / "18_empty_input_aggregate.sql").read_text(
            encoding="utf-8"
        )
    )
    n = _field(r, "n")
    assert _refs(n, "value_sources") == {"row_set:shop.main.orders"}
    assert "shop.main.orders.status" in _refs(n, "control_sources")
    assert "shop.main.orders.status" not in _refs(n, "value_sources")
    marker = _field(r, "marker")
    for bucket in ("value_sources", "control_sources", "existence_sources"):
        assert _refs(marker, bucket) == set()


def test_exists_over_constant_cardinality_has_no_sources(analyzer):
    # EXISTS(SELECT COUNT(*) FROM orders) 恒为真，与 orders 是否有行无关，
    # 因此不产生任何来源（不能把 COUNT 的行集标记当成存在性来源）。
    r = analyzer.analyze(
        "SELECT c.name AS customer_name FROM customers AS c "
        "WHERE EXISTS (SELECT COUNT(*) FROM orders AS o)"
    )
    name = _field(r, "customer_name")
    assert _refs(name, "value_sources") == {"shop.main.customers.name"}
    assert _refs(name, "control_sources") == set()
    assert _refs(name, "existence_sources") == set()


def test_empty_input_aggregate_null_value_traces_row_set(analyzer):
    # SUM/MIN/MAX 空输入返回 NULL（与标量子查询同一"行集为空"原因）；
    # COUNT 空输入返回 0，行集只作为取值来源(value)，不重复进 existence。
    sum_sql = "SELECT SUM(amount) AS s FROM orders WHERE status = 'PAID'"
    s = _field(analyzer.analyze(sum_sql), "s")
    assert _refs(s, "value_sources") == {"shop.main.orders.amount"}
    assert "shop.main.orders.status" in _refs(s, "control_sources")
    assert _refs(s, "existence_sources") == {"row_set:shop.main.orders"}

    count_sql = "SELECT COUNT(*) AS n FROM orders WHERE status = 'PAID'"
    n = _field(analyzer.analyze(count_sql), "n")
    assert _refs(n, "value_sources") == {"row_set:shop.main.orders"}
    assert "shop.main.orders.status" in _refs(n, "control_sources")
    assert _refs(n, "existence_sources") == set()
