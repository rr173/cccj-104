"""sqlrecon 结果对账回归测试（实际开 DuckDB 库核对，不做文字分析）。

覆盖：

* 清单校验：重名、缺字段、容差有限非负、键列禁容差、内联/文件两种 SQL 来源
* 静态护栏：写语句/CTE 内写操作/多语句/外部文件表函数/字符串关系/
  random/当前时间/非 SELECT 根，全部拒绝；WITH/UNION 等只读查询放行
* 标量语义：NULL、NaN、±Inf、精确大整数/DECIMAL 不走 float、容差边界包含
* 最大匹配：贪心陷阱（贪心失败但完整配对可行）、整行反例、一对一行不复用
* 确定性：行任意换序后结论、匹配数、差异展示完全一致
* 列结构：列数/列名/类型/顺序不符、重名列、找不到的键列/规则列分别报错
* 端到端：只读事务、空结果、错误与差异分开、单项失败继续其余项、退出码 0/1/2

运行：python3 -I -m pytest tests/test_sqlrecon.py -q
"""

from __future__ import annotations

import decimal
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

from sqlrecon import engine  # noqa: E402
from sqlrecon.compare import (  # noqa: E402
    ComparisonPlan,
    ColumnPlan,
    compare,
    maximum_matching,
)
from sqlrecon.encoding import encode_scalar  # noqa: E402
from sqlrecon.errors import (  # noqa: E402
    ManifestError,
    SchemaMismatchError,
    SQLError,
)
from sqlrecon.guard import parse_and_guard  # noqa: E402
from sqlrecon.manifest import CheckSpec, Tolerance, load_manifest  # noqa: E402
from sqlrecon.runner import (  # noqa: E402
    ColumnMeta,
    QueryResult,
    validate_named_columns,
    validate_schema,
)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def col(name: str, type_text: str, type_id: str | None = None) -> ColumnMeta:
    return ColumnMeta(
        name=name,
        type_text=type_text,
        type_id=type_id or type_text.split("(")[0].split("[")[0].lower(),
    )


def result(columns, rows) -> QueryResult:
    return QueryResult(columns=tuple(columns), rows=list(rows))


def spec(
    name="c",
    keys=(),
    tol=None,
    old="SELECT 1",
    new="SELECT 1",
) -> CheckSpec:
    return CheckSpec(
        name=name,
        old_sql=old,
        new_sql=new,
        keys=tuple(keys),
        tolerance=tol or {},
    )


# ---------------------------------------------------------------------------
# 1) 清单校验
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "checks.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_manifest_requires_nonempty_checks(tmp_path):
    p = _write_manifest(tmp_path, {"checks": []})
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_manifest_duplicate_names_rejected(tmp_path):
    p = _write_manifest(tmp_path, {
        "checks": [
            {"name": "x", "old_sql": "SELECT 1", "new_sql": "SELECT 1"},
            {"name": "x", "old_sql": "SELECT 1", "new_sql": "SELECT 1"},
        ]
    })
    with pytest.raises(ManifestError, match="重复"):
        load_manifest(p)


def test_manifest_missing_sql_rejected(tmp_path):
    p = _write_manifest(tmp_path, {
        "checks": [{"name": "x", "old_sql": "SELECT 1"}]
    })
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_manifest_sql_file_source(tmp_path):
    q = tmp_path / "a.sql"
    q.write_text("SELECT 1 AS x", encoding="utf-8")
    p = _write_manifest(tmp_path, {
        "checks": [{"name": "x", "old_sql_file": "a.sql",
                    "new_sql_file": "a.sql"}]
    })
    specs = load_manifest(p)
    assert specs[0].old_sql == "SELECT 1 AS x"


def test_manifest_tolerance_must_be_finite_nonnegative(tmp_path):
    for bad in (-1, "0.1", float("inf"), float("nan"), True):
        p = _write_manifest(tmp_path, {
            "checks": [{
                "name": "x",
                "old_sql": "SELECT 1 AS v",
                "new_sql": "SELECT 1 AS v",
                "tolerance": [{"column": "v", "abs": bad, "rel": 0}],
            }]
        })
        with pytest.raises(ManifestError):
            load_manifest(p)


def test_manifest_key_column_cannot_have_tolerance(tmp_path):
    p = _write_manifest(tmp_path, {
        "checks": [{
            "name": "x", "keys": ["k"],
            "old_sql": "SELECT 1 AS k", "new_sql": "SELECT 1 AS k",
            "tolerance": [{"column": "k", "abs": 0.1, "rel": 0}],
        }]
    })
    with pytest.raises(ManifestError, match="键列"):
        load_manifest(p)


def test_manifest_duplicate_tolerance_and_keys_rejected(tmp_path):
    p = _write_manifest(tmp_path, {
        "checks": [{
            "name": "x", "keys": ["k", "k"],
            "old_sql": "SELECT 1 AS k", "new_sql": "SELECT 1 AS k",
        }]
    })
    with pytest.raises(ManifestError):
        load_manifest(p)


# ---------------------------------------------------------------------------
# 2) 静态护栏
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql,reason", [
    ("INSERT INTO t VALUES (1)", "non_query_root"),
    ("CREATE VIEW v AS SELECT 1", "non_query_root"),
    ("SELECT * FROM t; DROP TABLE t", "multiple_statements"),
    ("SELECT * FROM t; SELECT * FROM u", "multiple_statements"),
    ("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "forbidden_node"),
    ("WITH x AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM x", "forbidden_node"),
    ("SELECT * FROM read_csv_auto('/tmp/x.csv')", "external_table_function"),
    ("SELECT * FROM parquet_scan('a.parquet')", "external_table_function"),
    ("SELECT * FROM 'a.parquet'", "external_file_relation"),
    ("SELECT random() FROM t", "nondeterministic"),
    ("SELECT rand() FROM t", "nondeterministic"),
    ("SELECT uuid() FROM t", "nondeterministic"),
    ("SELECT now() FROM t", "nondeterministic"),
    ("SELECT current_timestamp", "nondeterministic"),
    ("SELECT current_date", "nondeterministic"),
    ("WITH x AS (SELECT random() AS r FROM t) SELECT * FROM x", "nondeterministic"),
    ("ATTACH 'x.db' AS other", "non_query_root"),
    ("PRAGMA threads=1", "non_query_root"),
    ("COPY t TO 'out.parquet'", "non_query_root"),
    ("SELECT * FROM 's3://bucket/x.parquet'", "external_file_relation"),
    ("SELECT * FROM glob('/tmp/*.csv')", "external_table_function"),
    ("SELECT * FROM sqlite_scan('/tmp/x.db', 't')", "external_table_function"),
    ("VALUES (1), (2)", "non_query_root"),
    ("SELECT transaction_timestamp()", "nondeterministic"),
    ("SELECT localtimestamp", "nondeterministic"),
    ("WITH x AS (SELECT 1 AS c) SELECT * FROM read_json('/tmp/j.json')",
     "external_table_function"),
])
def test_guard_rejects(sql, reason):
    with pytest.raises(SQLError) as ei:
        parse_and_guard(sql)
    assert ei.value.details.get("reason") == reason or reason in str(ei.value)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t",
    "WITH x AS (SELECT * FROM t) SELECT * FROM x",
    "SELECT * FROM t UNION ALL SELECT * FROM u",
    "SELECT * FROM t INTERSECT SELECT * FROM u",
    "SELECT * FROM (VALUES (1,2)) v(a,b)",
    "SELECT * FROM generate_series(1, 3)",
    "SELECT count(*) FROM duckdb_tables()",
    # 普通列名即使取得像函数名也不允许误伤：这里没有任何函数调用
    "SELECT rnd FROM (SELECT 1 AS rnd) z",
    # 纯函数字面量转换不依赖运行时刻，放行
    "SELECT strftime(TIMESTAMP '2026-01-01 00:00:00', '%Y') AS y",
])
def test_guard_accepts_read_only_queries(sql):
    root = parse_and_guard(sql)
    assert root is not None


def test_guard_parse_error_reported():
    with pytest.raises(SQLError) as ei:
        parse_and_guard("SELECT FROM FROM")
    assert ei.value.details.get("reason") == "parse_error"


# ---------------------------------------------------------------------------
# 3) 值编码：精度与特殊值
# ---------------------------------------------------------------------------


def test_encode_huge_integer_stays_integer():
    encoded = encode_scalar(123456789012345678901234, "hugeint")
    assert encoded == {"t": "integer", "v": 123456789012345678901234}
    # JSON 往返不丢精度
    assert json.loads(json.dumps(encoded))["v"] == 123456789012345678901234


def test_encode_decimal_preserves_scale():
    import decimal
    encoded = encode_scalar(decimal.Decimal("123456789012345.123456"), "decimal")
    assert encoded == {"t": "decimal", "v": "123456789012345.123456"}


def test_encode_special_floats_are_distinct_markers():
    assert encode_scalar(float("nan"), "double")["v"] == "NaN"
    assert encode_scalar(float("inf"), "double")["v"] == "Infinity"
    assert encode_scalar(float("-inf"), "float")["v"] == "-Infinity"
    assert encode_scalar(None, "double") == {"t": "null", "v": None}
    # 标记是合法 JSON 字符串，不用 NaN 字面量
    for v in ("NaN", "Infinity", "-Infinity"):
        json.dumps({"t": "float", "v": v})


def test_encode_blob_and_temporal():
    import datetime
    import uuid
    assert encode_scalar(b"\x00\x01", "blob")["v"] == "AAE="
    enc = encode_scalar(datetime.datetime(2026, 3, 1, 10, 0), "timestamp")
    assert enc["t"] == "timestamp" and enc["v"].startswith("2026-03-01T10:00")
    enc = encode_scalar(uuid.UUID("4f47d6ba-3f69-4e26-92af-e1f0d07b1c99"), "uuid")
    assert enc["v"] == "4f47d6ba-3f69-4e26-92af-e1f0d07b1c99"


def test_encode_interval_and_timestamptz_tag():
    import datetime
    enc = encode_scalar(datetime.timedelta(days=3, seconds=4), "interval")
    assert enc == {"t": "interval", "v": "P3DT00H00M04.000000S"}
    # 带时区的 datetime 必须标成 timestamp_tz，即便 type_id 用含空格的全名
    tzdt = datetime.datetime(2026, 3, 1, 10, 0, tzinfo=datetime.timezone.utc)
    assert encode_scalar(tzdt, "timestamp with time zone")["t"] == "timestamp_tz"
    assert encode_scalar(tzdt, None)["t"] == "timestamp_tz"


def test_encode_nested_containers():
    enc = encode_scalar([1, 2], "list")
    assert enc["t"] == "list" and [p["v"] for p in enc["v"]] == [1, 2]
    enc = encode_scalar({"a": 1}, "map")
    assert enc["t"] == "map" and enc["v"][0]["key"]["v"] == "a"
    # 嵌套容器同样是合法 JSON（无 NaN 字面量泄漏）
    json.dumps(enc)


# ---------------------------------------------------------------------------
# 4) 列结构校验
# ---------------------------------------------------------------------------


def test_schema_column_count_mismatch():
    old = result([col("a", "INTEGER")], [(1,)])
    new = result([col("a", "INTEGER"), col("b", "INTEGER")], [(1, 2)])
    with pytest.raises(SchemaMismatchError) as ei:
        validate_schema(old, new, check_name="c")
    assert ei.value.details["reason"] == "column_count"


def test_schema_type_mismatch_not_stringified():
    old = result([col("a", "BIGINT", "bigint")], [(1,)])
    new = result([col("a", "VARCHAR", "varchar")], [("1",)])
    with pytest.raises(SchemaMismatchError) as ei:
        validate_schema(old, new, check_name="c")
    assert ei.value.details["reason"] == "column_signature"


def test_schema_decimal_scale_is_part_of_signature():
    old = result([col("a", "DECIMAL(10,2)", "decimal")], [])
    new = result([col("a", "DECIMAL(10,3)", "decimal")], [])
    with pytest.raises(SchemaMismatchError):
        validate_schema(old, new, check_name="c")


def test_schema_duplicate_column_rejected():
    old = result([col("a", "INTEGER"), col("a", "INTEGER")], [])
    new = result([col("a", "INTEGER"), col("a", "INTEGER")], [])
    with pytest.raises(SchemaMismatchError) as ei:
        validate_schema(old, new, check_name="c")
    assert ei.value.details["reason"] == "duplicate_column"


def test_unknown_key_and_rule_columns_rejected():
    cols = (col("id", "INTEGER"), col("v", "DOUBLE", "double"))
    with pytest.raises(SchemaMismatchError) as ei:
        validate_named_columns(cols, ("ghost",), (), check_name="c")
    assert ei.value.details["reason"] == "unknown_named_column"
    with pytest.raises(SchemaMismatchError) as ei:
        validate_named_columns(cols, (), ("ghost_metric",), check_name="c")
    assert ei.value.details["reason"] == "unknown_named_column"


def test_tolerance_on_non_numeric_rejected():
    cols = (col("id", "INTEGER"), col("name", "VARCHAR", "varchar"))
    with pytest.raises(SchemaMismatchError):
        validate_named_columns(cols, (), ("name",), check_name="c")


# ---------------------------------------------------------------------------
# 5) 标量与匹配语义（合成结果，不开库）
# ---------------------------------------------------------------------------


def _plan(columns, keys=(), tol=None) -> ComparisonPlan:
    key_names = set(keys)
    plans = []
    for i, c in enumerate(columns):
        plans.append(ColumnPlan(
            index=i, name=c.name, type_text=c.type_text, type_id=c.type_id,
            is_key=c.name in key_names,
            tolerance=None if c.name in key_names else (tol or {}).get(c.name),
        ))
    return ComparisonPlan(
        check_name="c", columns=tuple(plans),
        key_indexes=tuple(i for i, c in enumerate(columns) if c.name in key_names),
        value_indexes=tuple(i for i, c in enumerate(columns) if c.name not in key_names),
        grouped=bool(keys),
    )


def test_null_nan_infinity_identity():
    from sqlrecon.compare import _exact_equal
    assert _exact_equal(None, None, "double")
    assert not _exact_equal(None, 1.0, "double")
    assert _exact_equal(float("nan"), float("nan"), "double")
    assert not _exact_equal(float("nan"), 1.0, "double")
    assert _exact_equal(float("inf"), float("inf"), "double")
    assert not _exact_equal(float("inf"), float("-inf"), "double")
    assert not _exact_equal(float("-inf"), float("inf"), "double")


def test_exact_integer_never_float():
    from sqlrecon.compare import _exact_equal, _numeric_tolerant_equal
    big = 123456789012345678901234
    assert _exact_equal(big, big, "hugeint")
    assert not _exact_equal(big, big + 1, "hugeint")
    tol = Tolerance("d", abs=0.0, rel=0.0)
    d1 = decimal.Decimal("100.00")
    d2 = decimal.Decimal("100.00")
    assert _numeric_tolerant_equal(d1, d2, "decimal", tol)
    assert not _numeric_tolerant_equal(
        d1, decimal.Decimal("100.01"), "decimal", tol
    )


def test_tolerance_boundary_inclusive_decimal():
    from sqlrecon.compare import _numeric_tolerant_equal
    tol = Tolerance("d", abs=0.10, rel=0.0)
    # 差值恰好等于阈值：包含
    assert _numeric_tolerant_equal(
        decimal.Decimal("1.00"), decimal.Decimal("1.10"), "decimal", tol
    )
    # 超出 0.001：不通过
    assert not _numeric_tolerant_equal(
        decimal.Decimal("1.000"), decimal.Decimal("1.101"), "decimal", tol
    )


def test_relative_tolerance_formula():
    from sqlrecon.compare import _numeric_tolerant_equal
    # |100.10-100.00| = 0.10 ; r * max = 0.001 * 100.10 = 0.1001 -> 通过
    tol = Tolerance("d", abs=0.0, rel=0.001)
    assert _numeric_tolerant_equal(
        decimal.Decimal("100.00"), decimal.Decimal("100.10"), "decimal", tol
    )
    # r * max = 0.001 * 100.10 = 0.1001 < 0.101 -> 不通过
    assert not _numeric_tolerant_equal(
        decimal.Decimal("100.00"), decimal.Decimal("100.101"), "decimal", tol
    )


def test_float_tolerance_uses_float_math():
    from sqlrecon.compare import _numeric_tolerant_equal
    tol = Tolerance("f", abs=0.0, rel=0.01)
    assert _numeric_tolerant_equal(100.0, 100.5, "double", tol)  # 0.5 <= 1.005
    assert not _numeric_tolerant_equal(100.0, 102.0, "double", tol)


def test_greedy_trap_needs_maximum_matching():
    """左 [2.04,1.95] vs 右 [2.00,2.10]，abs=0.10：贪心 1 对，最大匹配 2 对。"""
    cols = (col("k", "VARCHAR", "varchar"), col("a", "DECIMAL(10,2)", "decimal"))
    plan = _plan(cols, keys=("k",), tol={"a": Tolerance("a", 0.10, 0.0)})
    left_rows = [("G", decimal.Decimal("2.04")), ("G", decimal.Decimal("1.95"))]
    right_rows = [("G", decimal.Decimal("2.00")), ("G", decimal.Decimal("2.10"))]
    match = maximum_matching([0, 1], [0, 1], left_rows, right_rows, plan)
    assert len(match) == 2  # 完整配对可行


def test_greedy_trap_greedy_indeed_fails():
    """独立验证贪心在该数据上确实只配出 1 对（证明测试场景有效）。"""
    left = [2.04, 1.95]
    right = [2.00, 2.10]
    remaining = list(right)
    greedy = 0
    for x in left:
        pick = min(remaining, key=lambda y: abs(x - y))
        if abs(x - pick) <= 0.10:
            remaining.remove(pick)
            greedy += 1
    assert greedy == 1


def test_whole_row_lock_anti_example():
    """每列单独都能配上，整行却配不上：matched=0。"""
    import decimal
    cols = (
        col("k", "VARCHAR", "varchar"),
        col("a", "DECIMAL(10,2)", "decimal"),
        col("b", "DECIMAL(10,2)", "decimal"),
    )
    plan = _plan(cols, keys=("k",))
    old = result(cols, [("H", decimal.Decimal("1.00"), decimal.Decimal("1.00")),
                        ("H", decimal.Decimal("2.00"), decimal.Decimal("2.00"))])
    new = result(cols, [("H", decimal.Decimal("1.00"), decimal.Decimal("2.00")),
                        ("H", decimal.Decimal("2.00"), decimal.Decimal("1.00"))])
    outcome = compare(spec(keys=("k",)), old, new)
    assert not outcome.equal
    assert outcome.total_matched_pairs == 0
    diff = outcome.differences[0]
    assert diff.left_unmatched and diff.right_unmatched


def test_one_row_cannot_match_two():
    """左边两行相同、右边只有一行：最多配 1 对，剩余 1 行。"""
    cols = (col("k", "VARCHAR", "varchar"), col("v", "INTEGER", "integer"))
    plan = _plan(cols, keys=("k",))
    old = result(cols, [("g", 1), ("g", 1)])
    new = result(cols, [("g", 1)])
    outcome = compare(spec(keys=("k",)), old, new)
    assert not outcome.equal
    assert outcome.total_matched_pairs == 1
    assert outcome.differences[0].left_unmatched[0]["count"] == 1


def test_null_key_groups_normally():
    cols = (col("k", "VARCHAR", "varchar"), col("v", "INTEGER", "integer"))
    plan = _plan(cols, keys=("k",))
    rows = [(None, 10), (None, 10), ("a", 1), ("a", 2)]
    old = result(cols, rows)
    new = result(cols, list(reversed(rows)))
    outcome = compare(spec(keys=("k",)), old, new)
    assert outcome.equal
    assert outcome.group_count == 2  # NULL 键自成一组


def test_no_keys_whole_table_one_group():
    cols = (col("a", "INTEGER", "integer"),)
    old = result(cols, [(1,), (2,), (2,)])
    new = result(cols, [(2,), (1,), (2,)])  # 同多重集合换序
    outcome = compare(spec(), old, new)
    assert outcome.equal
    assert outcome.group_count == 1


def test_difference_payload_keeps_raw_values_and_multiplicity():
    """配不满时给剩余行及重复次数，不拼造变化行。"""
    cols = (col("k", "VARCHAR", "varchar"), col("v", "INTEGER", "integer"))
    old = result(cols, [("g", 1), ("g", 1), ("g", 2)])
    new = result(cols, [("g", 1)])
    outcome = compare(spec(keys=("k",)), old, new)
    diff = outcome.differences[0]
    # 剩余的是两行：值 1 剩 1 行、值 2 剩 1 行（1 已被配对消耗 1 次）
    leftover = {item["row"]["v"]["v"]: item["count"]
                for item in diff.left_unmatched}
    assert leftover == {1: 1, 2: 1}
    assert diff.right_unmatched == []


def test_determinism_under_all_permutations():
    """同样数据任意换序：结论、匹配数、差异 JSON 完全一致。"""
    import decimal
    import random
    cols = (
        col("k", "VARCHAR", "varchar"),
        col("a", "DECIMAL(10,2)", "decimal"),
        col("b", "DECIMAL(10,2)", "decimal"),
    )
    base_left = [
        ("G", decimal.Decimal("1.00"), decimal.Decimal("1.00")),
        ("G", decimal.Decimal("1.00"), decimal.Decimal("1.00")),
        ("G", decimal.Decimal("2.04"), decimal.Decimal("9.00")),
        ("G", decimal.Decimal("1.95"), decimal.Decimal("9.00")),
    ]
    base_right = [
        ("G", decimal.Decimal("1.00"), decimal.Decimal("1.00")),
        ("G", decimal.Decimal("2.00"), decimal.Decimal("9.00")),
        ("G", decimal.Decimal("2.10"), decimal.Decimal("9.00")),
        ("G", decimal.Decimal("5.00"), decimal.Decimal("9.00")),
    ]
    s = spec(keys=("k",), tol={"a": Tolerance("a", 0.10, 0.0)})
    reference = compare(
        s, result(cols, base_left), result(cols, base_right)
    ).to_dict()
    rng = random.Random(7)
    for _ in range(20):
        l = list(base_left)
        r = list(base_right)
        rng.shuffle(l)
        rng.shuffle(r)
        got = compare(s, result(cols, l), result(cols, r)).to_dict()
        assert got == reference


# ---------------------------------------------------------------------------
# 6) 端到端：真实 DuckDB 库、只读事务、退出码、错误隔离
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "t.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (k VARCHAR, v DECIMAL(10,2), f DOUBLE)")
    con.executemany(
        "INSERT INTO t VALUES (?, ?, ?)",
        [
            ("g", 1.00, 1.0), ("g", 1.00, 1.0),
            ("g", 2.04, 2.0), ("g", 1.95, 2.0),
            ("h", 3.00, float("nan")),
        ],
    )
    con.execute("CREATE TABLE empty_t (k VARCHAR, v INTEGER)")
    con.close()
    return path


def _manifest(tmp_path, checks):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"checks": checks}), encoding="utf-8")
    return p


def test_end_to_end_equal_difference_error_exit_codes(db_path, tmp_path):
    p = _manifest(tmp_path, [
        {
            "name": "ok",
            "keys": ["k"],
            "old_sql": "SELECT k, v FROM t ORDER BY v",
            "new_sql": "SELECT k, v FROM t",
        },
        {
            "name": "diff",
            "keys": ["k"],
            "old_sql": "SELECT k, v FROM t",
            "new_sql": "SELECT k, v FROM t WHERE v < 2.0",
        },
        {
            "name": "boom",
            "old_sql": "SELECT * FROM no_such_table",
            "new_sql": "SELECT k, v FROM t",
        },
    ])
    report = engine.run_checklist(db_path, p)
    assert report["summary"] == {
        "checks_total": 3, "equal": 1, "difference": 1, "error": 1
    }
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses == {"ok": "equal", "diff": "difference", "boom": "error"}
    boom = next(c for c in report["checks"] if c["name"] == "boom")
    assert boom["error"]["code"] == "sql_execution_error"
    assert engine.exit_code_for(report) == 2


def test_all_equal_exit_zero(db_path, tmp_path):
    p = _manifest(tmp_path, [{
        "name": "ok",
        "old_sql": "SELECT k, v FROM t",
        "new_sql": "SELECT k, v FROM t ORDER BY k DESC, v DESC",
    }])
    report = engine.run_checklist(db_path, p)
    assert report["overall_status"] == "equal"
    assert engine.exit_code_for(report) == 0


def test_difference_only_exit_one(db_path, tmp_path):
    p = _manifest(tmp_path, [{
        "name": "d",
        "old_sql": "SELECT k FROM t",
        "new_sql": "SELECT k FROM t WHERE k = 'h'",
    }])
    report = engine.run_checklist(db_path, p)
    assert report["overall_status"] == "difference"
    assert engine.exit_code_for(report) == 1


def test_error_check_does_not_stop_later_checks(db_path, tmp_path):
    p = _manifest(tmp_path, [
        {"name": "bad", "old_sql": "SELECT random() FROM t",
         "new_sql": "SELECT k FROM t"},
        {"name": "good", "old_sql": "SELECT k FROM empty_t",
         "new_sql": "SELECT k FROM empty_t WHERE v > 0"},
    ])
    report = engine.run_checklist(db_path, p)
    statuses = [c["status"] for c in report["checks"]]
    assert statuses == ["error", "equal"]


def test_empty_results_are_equal_not_auto_pass(db_path, tmp_path):
    p = _manifest(tmp_path, [
        {
            "name": "both_empty",
            "old_sql": "SELECT k, v FROM empty_t",
            "new_sql": "SELECT k, v FROM empty_t WHERE v > 0",
        },
        {
            "name": "empty_vs_row",
            "old_sql": "SELECT k, v FROM empty_t",
            "new_sql": "SELECT k, v FROM empty_t UNION ALL SELECT 'x', 1",
        },
    ])
    report = engine.run_checklist(db_path, p)
    assert report["checks"][0]["status"] == "equal"
    assert report["checks"][1]["status"] == "difference"
    diff = report["checks"][1]["result"]["differences"][0]
    assert diff["right_unmatched"][0]["count"] == 1


def test_write_inside_cte_rejected_at_runtime_path(db_path, tmp_path):
    p = _manifest(tmp_path, [{
        "name": "write",
        "old_sql": (
            "WITH x AS (UPDATE t SET v = 0 RETURNING k) SELECT k FROM x"
        ),
        "new_sql": "SELECT k FROM t",
    }])
    report = engine.run_checklist(db_path, p)
    assert report["checks"][0]["status"] == "error"
    assert report["checks"][0]["error"]["code"] == "sql_rejected"
    # 库内容未被改动
    ro = duckdb.connect(str(db_path), read_only=True)
    assert ro.execute("SELECT count(*) FROM t").fetchone()[0] == 5
    ro.close()


def test_read_only_connection_blocks_writes(db_path, tmp_path):
    """第二道闸门：即使绕过静态检查，只读连接也拒绝写入。"""
    ro = duckdb.connect(str(db_path), read_only=True)
    with pytest.raises(duckdb.Error):
        ro.execute("DELETE FROM t")
    ro.close()


def test_missing_database_reported_as_command_error(tmp_path):
    from sqlrecon.errors import DatabaseOpenError
    manifest = _write_manifest(tmp_path, {
        "checks": [{"name": "x", "old_sql": "SELECT 1", "new_sql": "SELECT 1"}]
    })
    with pytest.raises(DatabaseOpenError):
        engine.run_checklist(tmp_path / "nope.duckdb", manifest)


def test_cli_subprocess_exit_codes(db_path, tmp_path):
    p = _manifest(tmp_path, [{
        "name": "d",
        "old_sql": "SELECT k FROM t",
        "new_sql": "SELECT k FROM t WHERE k = 'h'",
    }])
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "src/sqlrecon/cli.py"),
         "run", "--db", str(db_path), "--manifest", str(p)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["overall_status"] == "difference"


def test_same_read_transaction_snapshot(db_path, tmp_path):
    """两份查询在同一读取事务执行：构造可重复读的一致性（只读连接快照语义）。"""
    p = _manifest(tmp_path, [{
        "name": "snap",
        "old_sql": "SELECT count(*) AS n FROM t",
        "new_sql": "SELECT count(*) AS n FROM t",
    }])
    report = engine.run_checklist(db_path, p)
    assert report["checks"][0]["status"] == "equal"
    assert report["checks"][0]["result"]["matched_pairs"] == 1
