"""relslice 关系数据裁取回归测试（实际开 DuckDB 库核对，不做文字分析）。

覆盖：

* 配方校验：表名形状、重复表/引用/身份列、未声明表、起点值类型与个数
* 源结构核对：缺表/视图/缺列、身份列类型限整数或字符串、身份空值与
  重复身份（带示例值）、引用两侧列数与类型一致性
* 闭包算法（纯集合、内存假数据）：单向追踪、共用父记录、自引用/跨表环、
  复合引用部分为空、重复起点、断链链路、预算边界、排列不变性
* 端到端（真实库文件）：逐表核对身份与全字段值、空表保留结构、
  同名表跨 schema 不混、目标已存在拒绝、失败不留文件、源库只读、
  说明与库一致、CLI 退出码 0/1/2

运行：python3 -I -m pytest tests/test_relslice.py -q
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

from relslice.closure import compute_closure  # noqa: E402
from relslice.engine import extract  # noqa: E402
from relslice.errors import (  # noqa: E402
    BrokenReferenceError,
    BudgetExceededError,
    RecipeError,
    SeedNotFoundError,
    SourceSchemaError,
    TargetExistsError,
)
from relslice.recipe import ReferenceSpec, TableRef, load_recipe  # noqa: E402

CLI = ROOT / "src" / "relslice" / "cli.py"

C = TableRef("main", "customers")
O = TableRef("main", "orders")
I = TableRef("main", "items")
E = TableRef("main", "employees")


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def _write_recipe(tmp_path: Path, payload: dict, name: str = "recipe.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _recipe_payload(**over) -> dict:
    payload = {
        "tables": [
            {"name": "customers", "identity": ["cid"]},
            {"name": "orders", "identity": ["oid"]},
            {"name": "items", "identity": ["oid", "line"]},
        ],
        "references": [
            {"name": "orders_customer", "child": "orders",
             "child_columns": ["cid"], "parent": "customers"},
            {"name": "items_order", "child": "items",
             "child_columns": ["oid"], "parent": "orders"},
        ],
        "seeds": [{"table": "orders", "identity": [10]}],
    }
    payload.update(over)
    return payload


def _build_source(path: Path) -> None:
    """小型业务库：客户-订单-明细 + 自引用员工，含孤儿与断链记录。"""
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE customers (cid INTEGER, name VARCHAR)")
        con.execute("CREATE TABLE orders (oid INTEGER, cid INTEGER, note varchar)")
        con.execute("CREATE TABLE items (oid INTEGER, line INTEGER, sku varchar, qty INTEGER)")
        con.execute("CREATE TABLE employees (emp_id INTEGER, mgr_id INTEGER)")
        con.execute("INSERT INTO customers VALUES (1,'a'),(2,'b')")
        con.execute("INSERT INTO orders VALUES (10,1,'x'),(11,1,'y'),(12,2,'z'),(13,999,'broken')")
        con.execute("INSERT INTO items VALUES (10,1,'P1',2),(10,2,NULL,1),(11,1,'P2',1)")
        con.execute("INSERT INTO employees VALUES (1,2),(2,1),(3,NULL),(4,5)")
    finally:
        con.close()


def _run_cli(*args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), *args],
        capture_output=True, text=True, cwd=ROOT,
    )
    return proc.returncode, json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 1) 配方校验（纯 JSON 形状）
# ---------------------------------------------------------------------------


def test_recipe_requires_nonempty_tables(tmp_path):
    p = _write_recipe(tmp_path, {"tables": []})
    with pytest.raises(RecipeError):
        load_recipe(p)


def test_recipe_table_name_shapes(tmp_path):
    for bad in ("a.b.c", ".orders", "orders.", "", 123):
        p = _write_recipe(tmp_path, {"tables": [{"name": bad, "identity": ["id"]}]})
        with pytest.raises(RecipeError):
            load_recipe(p)


def test_recipe_unqualified_name_defaults_to_main(tmp_path):
    p = _write_recipe(tmp_path, {"tables": [{"name": "orders", "identity": ["oid"]}]})
    recipe = load_recipe(p)
    assert TableRef("main", "orders") in recipe.tables


def test_recipe_duplicate_table_after_normalization(tmp_path):
    p = _write_recipe(tmp_path, {"tables": [
        {"name": "orders", "identity": ["oid"]},
        {"name": "main.orders", "identity": ["oid"]},
    ]})
    with pytest.raises(RecipeError, match="重复"):
        load_recipe(p)


def test_recipe_identity_must_be_nonempty_unique_columns(tmp_path):
    p = _write_recipe(tmp_path, {"tables": [{"name": "t", "identity": []}]})
    with pytest.raises(RecipeError):
        load_recipe(p)
    p = _write_recipe(tmp_path, {"tables": [{"name": "t", "identity": ["a", "a"]}]})
    with pytest.raises(RecipeError, match="重复列名"):
        load_recipe(p)


def test_recipe_reference_validation(tmp_path):
    base = _recipe_payload()
    # 引用名重复
    dup_name = _recipe_payload(references=[
        {"name": "r", "child": "orders", "child_columns": ["cid"], "parent": "customers"},
        {"name": "r", "child": "items", "child_columns": ["oid"], "parent": "orders"},
    ])
    with pytest.raises(RecipeError, match="重复"):
        load_recipe(_write_recipe(tmp_path, dup_name))
    # 引用未声明的表
    undeclared = _recipe_payload(references=[
        {"name": "r", "child": "ghost", "child_columns": ["x"], "parent": "customers"},
    ])
    with pytest.raises(RecipeError, match="未在 tables 中声明"):
        load_recipe(_write_recipe(tmp_path, undeclared))
    # 完全重复的引用
    dup_shape = _recipe_payload(references=[
        {"name": "r1", "child": "orders", "child_columns": ["cid"], "parent": "customers"},
        {"name": "r2", "child": "orders", "child_columns": ["cid"], "parent": "customers"},
    ])
    with pytest.raises(RecipeError, match="完全重复"):
        load_recipe(_write_recipe(tmp_path, dup_shape))
    # 空列组
    empty_cols = _recipe_payload(references=[
        {"name": "r", "child": "orders", "child_columns": [], "parent": "customers"},
    ])
    with pytest.raises(RecipeError):
        load_recipe(_write_recipe(tmp_path, empty_cols))
    assert load_recipe(_write_recipe(tmp_path, base)).references  # 基准配方合法


def test_recipe_seed_validation(tmp_path):
    # 起点表未声明
    with pytest.raises(RecipeError, match="未在 tables 中声明"):
        load_recipe(_write_recipe(tmp_path, _recipe_payload(
            seeds=[{"table": "ghost", "identity": [1]}])))
    # 身份值个数与身份列数不符
    with pytest.raises(RecipeError, match="个数必须一致"):
        load_recipe(_write_recipe(tmp_path, _recipe_payload(
            seeds=[{"table": "items", "identity": [1]}])))
    # 身份值类型：null / 布尔 / 小数 / 数组 都拒绝
    for bad in (None, True, 1.5, [1]):
        with pytest.raises(RecipeError, match="整数或字符串"):
            load_recipe(_write_recipe(tmp_path, _recipe_payload(
                seeds=[{"table": "orders", "identity": [bad]}])))


def test_recipe_error_carries_location(tmp_path):
    p = _write_recipe(tmp_path, _recipe_payload(
        seeds=[{"table": "orders", "identity": [None]}]))
    try:
        load_recipe(p)
    except RecipeError as ex:
        assert ex.details["location"] == "seeds[0].identity[0]"
    else:  # pragma: no cover
        raise AssertionError("应抛出 RecipeError")


# ---------------------------------------------------------------------------
# 2) 源结构核对（真实 DuckDB）
# ---------------------------------------------------------------------------


def _extract(tmp_path: Path, recipe_payload: dict, **kw):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    recipe = _write_recipe(tmp_path, recipe_payload)
    return extract(src, recipe, tmp_path / "out.duckdb", **kw)


def test_source_missing_table(tmp_path):
    payload = _recipe_payload()
    payload["tables"].append({"name": "ghost", "identity": ["id"]})
    with pytest.raises(SourceSchemaError) as exc:
        _extract(tmp_path, payload)
    assert exc.value.details["location"] == "tables[3]"


def test_source_view_is_not_base_table(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    con = duckdb.connect(str(src))
    con.execute("CREATE VIEW v_orders AS SELECT * FROM orders")
    con.close()
    payload = _recipe_payload(
        tables=[{"name": "v_orders", "identity": ["oid"]}],
        references=[],
        seeds=[{"table": "v_orders", "identity": [10]}],
    )
    with pytest.raises(SourceSchemaError, match="不是基础表"):
        extract(src, _write_recipe(tmp_path, payload), tmp_path / "out.duckdb")


def test_source_identity_column_missing(tmp_path):
    payload = _recipe_payload()
    payload["tables"][0] = {"name": "customers", "identity": ["nope"]}
    with pytest.raises(SourceSchemaError) as exc:
        _extract(tmp_path, payload)
    assert exc.value.details["location"] == "tables[0].identity[0]"


def test_source_identity_type_restricted(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE t (id INTEGER, d DATE, w DOUBLE, b BOOLEAN)")
    con.execute("INSERT INTO t VALUES (1, DATE '2026-01-01', 1.5, true)")
    con.close()
    for col in ("d", "w", "b"):
        payload = {"tables": [{"name": "t", "identity": [col]}]}
        with pytest.raises(SourceSchemaError, match="限整数或字符串"):
            extract(src, _write_recipe(tmp_path, payload), tmp_path / f"out_{col}.duckdb")


def test_source_identity_null_and_duplicate(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE n (id INTEGER, v varchar)")
    con.execute("INSERT INTO n VALUES (1,'a'),(NULL,'b')")
    con.execute("CREATE TABLE d (id INTEGER, v varchar)")
    con.execute("INSERT INTO d VALUES (1,'a'),(1,'b')")
    con.close()
    with pytest.raises(SourceSchemaError, match="空值"):
        extract(src, _write_recipe(tmp_path, {
            "tables": [{"name": "n", "identity": ["id"]}]}), tmp_path / "o1.duckdb")
    with pytest.raises(SourceSchemaError, match="不唯一") as exc:
        extract(src, _write_recipe(tmp_path, {
            "tables": [{"name": "d", "identity": ["id"]}]}), tmp_path / "o2.duckdb")
    assert exc.value.details["duplicate_examples"] == [[1]]


def test_source_reference_column_and_arity_and_type(tmp_path):
    # 子表列不存在
    payload = _recipe_payload(references=[
        {"name": "r", "child": "orders", "child_columns": ["nope"], "parent": "customers"},
    ])
    with pytest.raises(SourceSchemaError) as exc:
        _extract(tmp_path, payload)
    assert exc.value.details["location"] == "references[0].child_columns[0]"
    # 列数不一致
    payload = _recipe_payload(references=[
        {"name": "r", "child": "items", "child_columns": ["oid", "line"], "parent": "customers"},
    ])
    with pytest.raises(SourceSchemaError, match="列数必须一致"):
        _extract(tmp_path, payload)
    # 类型不一致（varchar vs integer）
    payload = _recipe_payload(references=[
        {"name": "r", "child": "items", "child_columns": ["sku"], "parent": "customers"},
    ])
    with pytest.raises(SourceSchemaError, match="类型不一致"):
        _extract(tmp_path, payload)


def test_source_reference_type_mismatch_int_widths(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE p (id BIGINT)")
    con.execute("CREATE TABLE c (pid INTEGER)")
    con.execute("INSERT INTO p VALUES (1)")
    con.execute("INSERT INTO c VALUES (1)")
    con.close()
    payload = {
        "tables": [{"name": "p", "identity": ["id"]}, {"name": "c", "identity": ["pid"]}],
        "references": [{"name": "r", "child": "c", "child_columns": ["pid"], "parent": "p"}],
    }
    # INTEGER 与 BIGINT 不偷偷视为相容
    with pytest.raises(SourceSchemaError, match="类型不一致"):
        extract(src, _write_recipe(tmp_path, payload), tmp_path / "out.duckdb")


def test_seed_value_type_must_match_column(tmp_path):
    payload = _recipe_payload(seeds=[{"table": "orders", "identity": ["10"]}])
    with pytest.raises(RecipeError, match="JSON 整数"):
        _extract(tmp_path, payload)
    payload = _recipe_payload(
        tables=[{"name": "customers", "identity": ["name"]}],
        references=[],
        seeds=[{"table": "customers", "identity": [1]}],
    )
    with pytest.raises(RecipeError, match="JSON 字符串"):
        _extract(tmp_path, payload)


# ---------------------------------------------------------------------------
# 3) 闭包算法（纯集合，内存假数据，不开库）
# ---------------------------------------------------------------------------


def _ref(name, child, cols, parent):
    return ReferenceSpec(name=name, child=child, child_columns=tuple(cols), parent=parent)


def _fetch_from(data: dict):
    def fetch(ref: TableRef, identity: tuple):
        return data.get(ref, {}).get(identity)
    return fetch


def test_closure_basic_chain_and_no_reverse_pull():
    data = {
        C: {(1,): {"cid": 1}, (2,): {"cid": 2}},
        O: {(10,): {"oid": 10, "cid": 1}, (11,): {"oid": 11, "cid": 1}},
        I: {(10, 1): {"oid": 10, "line": 1}},
    }
    refs = (_ref("orders_customer", O, ["cid"], C), _ref("items_order", I, ["oid"], O))
    # 从订单出发：带上客户，不带客户的其他订单，也不带自己的明细
    got = compute_closure(tables=(C, O, I), references=refs, seeds=((O, (10,)),),
                          fetch_row=_fetch_from(data))
    assert set(got.selected[O]) == {(10,)}
    assert set(got.selected[C]) == {(1,)}
    assert set(got.selected[I]) == set()
    # 从客户出发：不沿反方向带订单
    got = compute_closure(tables=(C, O, I), references=refs, seeds=((C, (1,)),),
                          fetch_row=_fetch_from(data))
    assert set(got.selected[O]) == set()
    assert set(got.selected[C]) == {(1,)}


def test_closure_shared_parent_once_and_cycle():
    data = {
        E: {
            (1,): {"emp": 1, "mgr": 2},
            (2,): {"emp": 2, "mgr": 1},  # 1<->2 互引
            (3,): {"emp": 3, "mgr": 2},
        },
    }
    refs = (_ref("emp_mgr", E, ["mgr"], E),)
    got = compute_closure(tables=(E,), references=refs,
                          seeds=((E, (1,)), (E, (3,)), (E, (3,))),
                          fetch_row=_fetch_from(data))
    assert set(got.selected[E]) == {(1,), (2,), (3,)}  # 环收敛、重复起点去重
    assert got.total == 3


def test_closure_cross_table_cycle():
    a, b = TableRef("main", "a"), TableRef("main", "b")
    data = {
        a: {(1,): {"id": 1, "bid": 10}},
        b: {(10,): {"id": 10, "aid": 1}},
    }
    refs = (_ref("a_b", a, ["bid"], b), _ref("b_a", b, ["aid"], a))
    got = compute_closure(tables=(a, b), references=refs, seeds=((a, (1,)),),
                          fetch_row=_fetch_from(data))
    assert set(got.selected[a]) == {(1,)} and set(got.selected[b]) == {(10,)}


def test_closure_null_components_skip_parent():
    data = {
        O: {(10,): {"oid": 10, "cid": None}},
        I: {(10, 1): {"oid": 10, "line": 1, "sku": None, "region": "n"},
            (10, 2): {"oid": 10, "line": 2, "sku": "P1", "region": None}},
        C: {(1,): {"cid": 1}},
    }
    p = TableRef("main", "price")
    data[p] = {}
    refs = (
        _ref("orders_customer", O, ["cid"], C),
        _ref("items_price", I, ["region", "sku"], p),
    )
    got = compute_closure(tables=(O, I, C, p), references=refs,
                          seeds=((O, (10,)), (I, (10, 1)), (I, (10, 2))),
                          fetch_row=_fetch_from(data))
    assert set(got.selected[C]) == set()  # 单列引用为空 -> 不补
    assert set(got.selected[p]) == set()  # 复合引用部分为空 -> 不补


def test_closure_seed_not_found_and_broken_trail():
    data = {
        C: {},
        O: {(10,): {"oid": 10, "cid": 999}},
        I: {(10, 1): {"oid": 10, "line": 1}},
    }
    refs = (_ref("orders_customer", O, ["cid"], C), _ref("items_order", I, ["oid"], O))
    with pytest.raises(SeedNotFoundError):
        compute_closure(tables=(C, O, I), references=refs, seeds=((O, (42,)),),
                        fetch_row=_fetch_from(data))
    with pytest.raises(BrokenReferenceError) as exc:
        compute_closure(tables=(C, O, I), references=refs, seeds=((I, (10, 1)),),
                        fetch_row=_fetch_from(data))
    err = exc.value.details
    assert err["reference"] == "orders_customer"
    assert err["missing_parent_identity"] == [999]
    assert err["trail"]["seed"] == {"table": "main.items", "identity": [10, 1]}
    assert [s["reference"] for s in err["trail"]["path"]] == ["items_order"]


def test_closure_budget_exact_and_exceeded():
    data = {
        C: {(1,): {"cid": 1}},
        O: {(10,): {"oid": 10, "cid": 1}},
    }
    refs = (_ref("orders_customer", O, ["cid"], C),)
    got = compute_closure(tables=(C, O), references=refs, seeds=((O, (10,)),),
                          fetch_row=_fetch_from(data), budget=2)
    assert got.total == 2
    with pytest.raises(BudgetExceededError) as exc:
        compute_closure(tables=(C, O), references=refs, seeds=((O, (10,)),),
                        fetch_row=_fetch_from(data), budget=1)
    assert exc.value.details["budget"] == 1
    assert exc.value.details["confirmed"] == 1
    # 预算 0 + 空起点 -> 空闭包成功
    got = compute_closure(tables=(C, O), references=refs, seeds=(),
                          fetch_row=_fetch_from(data), budget=0)
    assert got.total == 0


def test_closure_permutation_invariance():
    data = {
        C: {(1,): {"cid": 1}, (2,): {"cid": 2}},
        O: {(10,): {"oid": 10, "cid": 1}, (11,): {"oid": 11, "cid": 2}},
    }
    refs = (_ref("orders_customer", O, ["cid"], C),)
    kw = dict(tables=(C, O), references=refs, fetch_row=_fetch_from(data))
    g1 = compute_closure(seeds=((O, (10,)), (O, (11,))), **kw)
    g2 = compute_closure(seeds=((O, (11,)), (O, (10,))), **kw)
    assert {t: set(s) for t, s in g1.selected.items()} == {t: set(s) for t, s in g2.selected.items()}


# ---------------------------------------------------------------------------
# 4) 端到端（真实库文件）
# ---------------------------------------------------------------------------


def test_end_to_end_target_contents(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    recipe = _write_recipe(tmp_path, _recipe_payload(
        seeds=[{"table": "items", "identity": [10, 1]},
               {"table": "items", "identity": [10, 2]}],
    ))
    out = tmp_path / "out.duckdb"
    report = extract(src, recipe, out)
    con = duckdb.connect(str(out), read_only=True)
    try:
        # 逐表核对身份集合与全字段值（不只核对条数）
        assert con.execute("SELECT * FROM customers").fetchall() == [(1, "a")]
        assert con.execute("SELECT * FROM orders").fetchall() == [(10, 1, "x")]
        assert sorted(con.execute("SELECT * FROM items").fetchall()) == [
            (10, 1, "P1", 2), (10, 2, None, 1)]
    finally:
        con.close()
    # 说明与目标库对得上
    by_table = {t["table"]: t for t in report["tables"]}
    assert {tuple(s["identity"]) for s in by_table["main.orders"]["selected"]} == {(10,)}
    cust = by_table["main.customers"]["selected"][0]
    assert cust["origin"]["kind"] == "derived"
    assert [p["reference"] for p in cust["origin"]["path"]] == ["items_order", "orders_customer"]


def test_end_to_end_schema_qualified_names_not_mixed(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE SCHEMA a")
    con.execute("CREATE SCHEMA b")
    con.execute("CREATE TABLE a.t (id INTEGER, v varchar)")
    con.execute("CREATE TABLE b.t (id INTEGER, v varchar)")
    con.execute("INSERT INTO a.t VALUES (1, 'A1'), (2, 'A2')")
    con.execute("INSERT INTO b.t VALUES (1, 'B1'), (2, 'B2')")
    con.close()
    payload = {
        "tables": [{"name": "a.t", "identity": ["id"]}, {"name": "b.t", "identity": ["id"]}],
        "seeds": [{"table": "a.t", "identity": [1]}],
    }
    out = tmp_path / "out.duckdb"
    extract(src, _write_recipe(tmp_path, payload), out)
    con = duckdb.connect(str(out), read_only=True)
    try:
        assert con.execute("SELECT * FROM a.t").fetchall() == [(1, "A1")]
        assert con.execute("SELECT * FROM b.t").fetchall() == []  # 同名异库表不混
    finally:
        con.close()


def test_end_to_end_field_types_and_values_carried(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE t (id INTEGER, price DECIMAL(10,2), d DATE, "
        "ts TIMESTAMP, ok BOOLEAN, note varchar)"
    )
    con.execute(
        "INSERT INTO t VALUES (1, 12.34, DATE '2026-01-02', "
        "TIMESTAMP '2026-01-02 03:04:05', true, 'n'),"
        "(2, 56.78, DATE '2026-01-03', "
        "TIMESTAMP '2026-01-03 04:05:06', false, NULL)"
    )
    con.close()
    payload = {"tables": [{"name": "t", "identity": ["id"]}],
               "seeds": [{"table": "t", "identity": [1]}]}
    out = tmp_path / "out.duckdb"
    extract(src, _write_recipe(tmp_path, payload), out)
    con = duckdb.connect(str(out), read_only=True)
    try:
        desc = {d[0]: str(d[1]) for d in con.execute("SELECT * FROM t LIMIT 0").description}
        assert desc == {"id": "INTEGER", "price": "DECIMAL(10,2)", "d": "DATE",
                        "ts": "TIMESTAMP", "ok": "BOOLEAN", "note": "VARCHAR"}
        rows = con.execute("SELECT * FROM t").fetchall()
        assert len(rows) == 1 and rows[0][0] == 1 and str(rows[0][1]) == "12.34"
    finally:
        con.close()


def test_end_to_end_empty_seeds(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    recipe = _write_recipe(tmp_path, _recipe_payload(seeds=[]))
    out = tmp_path / "out.duckdb"
    report = extract(src, recipe, out)
    assert report["budget"]["used"] == 0
    con = duckdb.connect(str(out), read_only=True)
    try:
        for t in ("customers", "orders", "items"):
            assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
        # 空表也保留完整字段结构
        desc = con.execute("SELECT * FROM items LIMIT 0").description
        assert [d[0] for d in desc] == ["oid", "line", "sku", "qty"]
        assert str(desc[3][1]) == "INTEGER"
    finally:
        con.close()


def test_target_exists_refused_and_untouched(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    recipe = _write_recipe(tmp_path, _recipe_payload())
    out = tmp_path / "out.duckdb"
    extract(src, recipe, out)
    before = hashlib.sha256(out.read_bytes()).hexdigest()
    with pytest.raises(TargetExistsError):
        extract(src, recipe, out)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == before
    # 目标位置是已存在目录同样拒绝
    with pytest.raises(TargetExistsError):
        extract(src, recipe, tmp_path)


def test_failure_leaves_no_target_file(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    out = tmp_path / "out.duckdb"
    # 断链：订单 13 -> 客户 999 不存在
    recipe = _write_recipe(tmp_path, _recipe_payload(
        seeds=[{"table": "orders", "identity": [13]}]))
    with pytest.raises(BrokenReferenceError):
        extract(src, recipe, out)
    assert not out.exists()
    assert not list(tmp_path.glob("out.duckdb.tmp-*"))
    # 预算不足同样不留文件
    recipe = _write_recipe(tmp_path, _recipe_payload(), "recipe2.json")
    with pytest.raises(BudgetExceededError):
        extract(src, recipe, out, budget=1)
    assert not out.exists()


def test_source_opened_read_only(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    recipe = _write_recipe(tmp_path, _recipe_payload())
    extract(src, recipe, tmp_path / "out.duckdb")
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_orphans_do_not_block_extraction(tmp_path):
    # 未被选中部分有断链孤儿（订单 13 -> 客户 999），完整裁取不受影响
    src = tmp_path / "src.duckdb"
    _build_source(src)
    recipe = _write_recipe(tmp_path, _recipe_payload(
        seeds=[{"table": "orders", "identity": [10]}]))
    report = extract(src, recipe, tmp_path / "out.duckdb")
    assert report["budget"]["used"] == 2  # 订单 10 + 客户 1


def test_employees_self_reference_cycle_end_to_end(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    payload = {
        "tables": [{"name": "employees", "identity": ["emp_id"]}],
        "references": [{"name": "mgr", "child": "employees",
                        "child_columns": ["mgr_id"], "parent": "employees"}],
        "seeds": [{"table": "employees", "identity": [1]}],
    }
    out = tmp_path / "out.duckdb"
    extract(src, _write_recipe(tmp_path, payload), out)
    con = duckdb.connect(str(out), read_only=True)
    try:
        got = {r[0] for r in con.execute("SELECT emp_id FROM employees").fetchall()}
        assert got == {1, 2}  # 1<->2 环收敛；3/4/5 不相关不入选
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 5) CLI 退出码
# ---------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    good = _write_recipe(tmp_path, _recipe_payload())
    out = tmp_path / "out.duckdb"
    rc, rep = _run_cli("extract", "--source", str(src), "--recipe", str(good),
                       "--target", str(out))
    assert rc == 0 and rep["status"] == "ok"
    # 目标已存在 -> 2
    rc, rep = _run_cli("extract", "--source", str(src), "--recipe", str(good),
                       "--target", str(out))
    assert rc == 2 and rep["error"]["code"] == "target_exists"
    # 断链 -> 1
    broken = _write_recipe(tmp_path, _recipe_payload(
        seeds=[{"table": "orders", "identity": [13]}]), "broken.json")
    rc, rep = _run_cli("extract", "--source", str(src), "--recipe", str(broken),
                       "--target", str(tmp_path / "o2.duckdb"))
    assert rc == 1 and rep["error"]["code"] == "broken_reference"
    # 起点缺失 -> 1
    missing = _write_recipe(tmp_path, _recipe_payload(
        seeds=[{"table": "orders", "identity": [42]}]), "missing.json")
    rc, rep = _run_cli("extract", "--source", str(src), "--recipe", str(missing),
                       "--target", str(tmp_path / "o3.duckdb"))
    assert rc == 1 and rep["error"]["code"] == "seed_not_found"
    # 预算不足 -> 1
    rc, rep = _run_cli("extract", "--source", str(src), "--recipe", str(good),
                       "--target", str(tmp_path / "o4.duckdb"), "--budget", "1")
    assert rc == 1 and rep["error"]["code"] == "budget_exceeded"
    # 配方不合法 -> 2
    bad = _write_recipe(tmp_path, {"tables": []}, "bad.json")
    rc, rep = _run_cli("extract", "--source", str(src), "--recipe", str(bad),
                       "--target", str(tmp_path / "o5.duckdb"))
    assert rc == 2 and rep["error"]["code"] == "recipe_invalid"
    # 源库不存在 -> 2
    rc, rep = _run_cli("extract", "--source", str(tmp_path / "nope.duckdb"),
                       "--recipe", str(good), "--target", str(tmp_path / "o6.duckdb"))
    assert rc == 2 and rep["error"]["code"] == "database_open_error"


def test_cli_report_file_and_budget_used(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_source(src)
    recipe = _write_recipe(tmp_path, _recipe_payload())
    out = tmp_path / "out.duckdb"
    report_path = tmp_path / "report.json"
    rc, rep = _run_cli("extract", "--source", str(src), "--recipe", str(recipe),
                       "--target", str(out), "--budget", "5",
                       "--report", str(report_path))
    assert rc == 0
    assert rep["budget"] == {"limit": 5, "used": 2}
    assert json.loads(report_path.read_text(encoding="utf-8")) == rep
