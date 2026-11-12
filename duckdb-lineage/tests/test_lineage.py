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
