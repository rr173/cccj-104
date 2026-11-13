"""relslice 值保真与一致性回归：裁取链上 DuckDB 逻辑类型与原值不丢。

针对的缺陷：旧搬运链把行取成 Python 对象再重新插入——TIMESTAMP_NS 纳秒
被截断（datetime 只到微秒）、INTERVAL 月份成分被换算成固定 30 天
（timedelta），导致裁取库里 ``DATE + term`` 的结果与源库不同。

核对方式（与"取成 Python 对象再比较"严格区分）：

* 裁取后 **重新打开** 源库与目标库，挂到同一个核对会话；
* 时间精度用 ``epoch_ns``、间隔用 ``datepart`` 的 月/日/微秒 成分、日期
  运算结果用数据库内 ``IS NOT DISTINCT FROM`` 等精确表达式核对——
  不把两边的时间/间隔取成可能已丢信息的 Python 对象再说相等
  （DuckDB 的 INTERVAL 等值会把 1 month 与 30 days 归一化判等，Python
  的 datetime/timedelta 更是装不下原值，两条路都会放过缺陷）；
* 断言只依赖源库/目标库与 SQL 字面量，不调用正式搬运函数本身。

覆盖：单行复现例、负值/跨月末/空值、列表与结构体里的时间/间隔、
含无关记录与父记录补入与空表的配方、精确整数/DECIMAL/BLOB、
一致读取（选择+搬运同一快照）、写出中途真实失败后的现场与恢复。

运行：python3 -I -m pytest tests/test_relslice_fidelity.py -q
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
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

import relslice.engine as engine_mod  # noqa: E402
from relslice.engine import extract  # noqa: E402
from relslice.errors import TargetWriteError  # noqa: E402

CLI = ROOT / "src" / "relslice" / "cli.py"


def _write_recipe(tmp_path: Path, payload: dict, name: str = "recipe.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _attach_pair(src: Path, out: Path) -> duckdb.DuckDBPyConnection:
    """重新打开源库与目标库，挂到同一个核对会话（s=源, t=目标）。"""
    con = duckdb.connect()
    con.execute("ATTACH $1 AS s (READ_ONLY)", [str(src)])
    con.execute("ATTACH $1 AS t (READ_ONLY)", [str(out)])
    return con


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1) 单行复现例：TIMESTAMP_NS 纳秒 + INTERVAL '1 month'
# ---------------------------------------------------------------------------


def test_single_row_timestamp_ns_and_interval_fidelity(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE events (id INTEGER, occurred TIMESTAMP_NS, term INTERVAL)"
    )
    con.execute(
        "INSERT INTO events VALUES "
        "(1, TIMESTAMP_NS '2026-02-01 12:00:00.123456789', INTERVAL '1 month')"
    )
    con.close()
    recipe = _write_recipe(tmp_path, {
        "tables": [{"name": "events", "identity": ["id"]}],
        "seeds": [{"table": "events", "identity": [1]}],
    })
    out = tmp_path / "out.duckdb"
    extract(src, recipe, out)

    chk = _attach_pair(src, out)
    try:
        desc = {
            d[0]: str(d[1])
            for d in chk.execute("SELECT * FROM t.main.events LIMIT 0").description
        }
        assert desc == {"id": "INTEGER", "occurred": "TIMESTAMP_NS", "term": "INTERVAL"}
        # 纳秒：数据库内精确等值（源与目标互等，且等于字面量原值）
        assert chk.execute(
            "SELECT (SELECT occurred FROM s.main.events WHERE id = 1) "
            "= (SELECT occurred FROM t.main.events WHERE id = 1) "
            "AND (SELECT occurred FROM t.main.events WHERE id = 1) "
            "= TIMESTAMP_NS '2026-02-01 12:00:00.123456789'"
        ).fetchone()[0] is True
        assert chk.execute(
            "SELECT epoch_ns(occurred) FROM t.main.events WHERE id = 1"
        ).fetchone()[0] == 1769947200123456789
        # 间隔成分：月/日/微秒逐项相等，且就是 1 month
        assert chk.execute(
            "SELECT datepart('year', term), datepart('month', term), "
            "datepart('day', term), datepart('microsecond', term) "
            "FROM t.main.events WHERE id = 1"
        ).fetchone() == (0, 1, 0, 0)
        # 后续日期运算：源库与目标库结果一致，且是 3 月 1 日而非 3 月 3 日
        assert chk.execute(
            "SELECT (SELECT DATE '2026-02-01' + term FROM s.main.events WHERE id = 1) "
            "= (SELECT DATE '2026-02-01' + term FROM t.main.events WHERE id = 1) "
            "AND (SELECT DATE '2026-02-01' + term FROM t.main.events WHERE id = 1) "
            "= TIMESTAMP '2026-03-01 00:00:00'"
        ).fetchone()[0] is True
    finally:
        chk.close()


# ---------------------------------------------------------------------------
# 2) 负值、跨月末、空值（列名刻意不同于复现例，防止只为两个列名打补丁）
# ---------------------------------------------------------------------------


def test_negative_month_end_and_null_values(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE measurements "
        "(mid BIGINT, base DATE, at_ns TIMESTAMP_NS, span INTERVAL)"
    )
    con.execute(
        "INSERT INTO measurements VALUES "
        # 正值
        "(1, DATE '2026-02-01', TIMESTAMP_NS '2026-02-01 12:00:00.123456789',"
        " INTERVAL '1 month'),"
        # 负值：负间隔成分 + 纪元前纳秒时间戳（epoch_ns 为负）
        "(2, DATE '2026-03-15', TIMESTAMP_NS '1969-12-31 23:59:59.999999999',"
        " -(INTERVAL '1 month 2 days 3 microseconds')),"
        # 空值
        "(3, NULL, NULL, NULL),"
        # 跨月末：1 月 31 日 + 1 month 应钳到 2 月 28 日（+30 days 则是 3 月 2 日）
        "(4, DATE '2026-01-31', TIMESTAMP_NS '2026-12-31 00:00:00.000000001',"
        " INTERVAL '1 month'),"
        # 未被选中的无关记录
        "(5, DATE '2026-05-01', TIMESTAMP_NS '2026-05-01 00:00:00.000000005',"
        " INTERVAL '5 days')"
    )
    con.close()
    recipe = _write_recipe(tmp_path, {
        "tables": [{"name": "measurements", "identity": ["mid"]}],
        "seeds": [{"table": "measurements", "identity": [i]} for i in (1, 2, 3, 4)],
    })
    out = tmp_path / "out.duckdb"
    extract(src, recipe, out)

    chk = _attach_pair(src, out)
    try:
        # 只输出所需集合：无关记录 5 不在
        assert chk.execute("SELECT count(*) FROM t.main.measurements").fetchone()[0] == 4
        # 目标库内逐行的精确成分（整数/None，无损取回）
        rows = chk.execute(
            "SELECT mid, epoch_ns(at_ns), datepart('year', span), "
            "datepart('month', span), datepart('day', span), "
            "datepart('microsecond', span) FROM t.main.measurements ORDER BY mid"
        ).fetchall()
        assert rows == [
            (1, 1769947200123456789, 0, 1, 0, 0),
            (2, -1, 0, -1, -2, -3),
            (3, None, None, None, None, None),
            (4, 1798675200000000001, 0, 1, 0, 0),
        ]
        # 与源库逐行数据库内比对：时间戳、间隔各成分、后续日期运算
        assert chk.execute(
            "SELECT count(*) FROM s.main.measurements s "
            "JOIN t.main.measurements t USING (mid) "
            "WHERE s.at_ns IS NOT DISTINCT FROM t.at_ns "
            "AND datepart('month', s.span) IS NOT DISTINCT FROM datepart('month', t.span) "
            "AND datepart('day', s.span) IS NOT DISTINCT FROM datepart('day', t.span) "
            "AND datepart('microsecond', s.span) IS NOT DISTINCT FROM "
            "datepart('microsecond', t.span) "
            "AND (s.base + s.span) IS NOT DISTINCT FROM (t.base + t.span)"
        ).fetchone()[0] == 4
        # 跨月末结果锚定：1 月 31 日 + 1 month = 2 月 28 日
        assert chk.execute(
            "SELECT (base + span) = TIMESTAMP '2026-02-28 00:00:00' "
            "FROM t.main.measurements WHERE mid = 4"
        ).fetchone()[0] is True
        # 负间隔的日期运算方向也一致
        assert chk.execute(
            "SELECT (SELECT base + span FROM s.main.measurements WHERE mid = 2) "
            "= (SELECT base + span FROM t.main.measurements WHERE mid = 2)"
        ).fetchone()[0] is True
    finally:
        chk.close()


# ---------------------------------------------------------------------------
# 3) 列表与结构体里的时间/间隔：整行搬运，不为特定列名打补丁
# ---------------------------------------------------------------------------


def test_nested_time_and_interval_in_list_and_struct(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE nest (id INTEGER, "
        "tags TIMESTAMP_NS[], "
        "st STRUCT(a TIMESTAMP_NS, b INTERVAL), "
        "ivs INTERVAL[])"
    )
    con.execute(
        "INSERT INTO nest VALUES "
        "(1, [TIMESTAMP_NS '2026-02-01 12:00:00.123456789', NULL],"
        " {'a': TIMESTAMP_NS '1969-12-31 23:59:59.999999999',"
        "  'b': INTERVAL '1 month 2 days'},"
        " [INTERVAL '1 month', INTERVAL '-2 days', NULL]),"
        "(2, [], NULL, NULL),"
        "(3, NULL, {'a': NULL, 'b': NULL}, [])"
    )
    con.close()
    recipe = _write_recipe(tmp_path, {
        "tables": [{"name": "nest", "identity": ["id"]}],
        "seeds": [{"table": "nest", "identity": [1]},
                  {"table": "nest", "identity": [2]},
                  {"table": "nest", "identity": [3]}],
    })
    out = tmp_path / "out.duckdb"
    extract(src, recipe, out)

    chk = _attach_pair(src, out)
    try:
        desc = {
            d[0]: str(d[1])
            for d in chk.execute("SELECT * FROM t.main.nest LIMIT 0").description
        }
        assert desc == {
            "id": "INTEGER",
            "tags": "TIMESTAMP_NS[]",
            "st": "STRUCT(a TIMESTAMP_NS, b INTERVAL)",
            "ivs": "INTERVAL[]",
        }
        # 嵌套成分逐一核对（整数/布尔/None，无损）
        row1 = chk.execute(
            "SELECT epoch_ns(tags[1]), tags[2] IS NULL, epoch_ns(st.a), "
            "datepart('month', st.b), datepart('day', st.b), "
            "datepart('month', ivs[1]), datepart('day', ivs[2]), ivs[3] IS NULL "
            "FROM t.main.nest WHERE id = 1"
        ).fetchone()
        assert row1 == (1769947200123456789, True, -1, 1, 2, 1, -2, True)
        # 空值/空列表/空结构体的区分不丢（length 计 NULL 元素，list_count 不计）
        rows = chk.execute(
            "SELECT id, tags IS NULL, st IS NULL, ivs IS NULL, "
            "length(tags), length(ivs) FROM t.main.nest ORDER BY id"
        ).fetchall()
        assert rows == [
            (1, False, False, False, 2, 3),
            (2, False, True, True, 0, None),
            (3, True, False, False, None, 0),
        ]
        # 与源库数据库内比对：纳秒列表逐元素精确等值（含 NULL 元素位置）
        assert chk.execute(
            "SELECT count(*) FROM s.main.nest s JOIN t.main.nest t USING (id) "
            "WHERE s.tags IS NOT DISTINCT FROM t.tags "
            "AND epoch_ns(s.st.a) IS NOT DISTINCT FROM epoch_ns(t.st.a) "
            "AND datepart('month', s.st.b) IS NOT DISTINCT FROM datepart('month', t.st.b) "
            "AND datepart('day', s.st.b) IS NOT DISTINCT FROM datepart('day', t.st.b)"
        ).fetchone()[0] == 3
    finally:
        chk.close()


# ---------------------------------------------------------------------------
# 4) 闭包语义不变：无关记录不带入、父记录补入且保真、空表保留结构
# ---------------------------------------------------------------------------


def test_closure_semantics_and_parent_fidelity(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE customers (cid INTEGER, name VARCHAR, "
        "vip_since TIMESTAMP_NS, note INTERVAL)"
    )
    con.execute(
        "CREATE TABLE orders (oid INTEGER, cid INTEGER, "
        "placed_at TIMESTAMP_NS, ttl INTERVAL)"
    )
    con.execute("CREATE TABLE audit_log (aid INTEGER, at_ns TIMESTAMP_NS)")
    con.execute(
        "INSERT INTO customers VALUES "
        "(1, 'a', TIMESTAMP_NS '2020-01-01 00:00:00.000000001', INTERVAL '3 months'),"
        "(2, 'b', TIMESTAMP_NS '2021-06-15 12:30:00.999999999', INTERVAL '-1 month')"
    )
    con.execute(
        "INSERT INTO orders VALUES "
        "(10, 1, TIMESTAMP_NS '2026-02-01 12:00:00.123456789', INTERVAL '1 month'),"
        "(11, 1, TIMESTAMP_NS '2026-02-02 00:00:00.000000002', INTERVAL '2 days'),"
        "(12, 2, TIMESTAMP_NS '2026-02-03 00:00:00.000000003', INTERVAL '1 year')"
    )
    con.close()
    recipe = _write_recipe(tmp_path, {
        "tables": [
            {"name": "customers", "identity": ["cid"]},
            {"name": "orders", "identity": ["oid"]},
            {"name": "audit_log", "identity": ["aid"]},
        ],
        "references": [
            {"name": "orders_customer", "child": "orders",
             "child_columns": ["cid"], "parent": "customers"},
        ],
        "seeds": [{"table": "orders", "identity": [10]}],
    })
    out = tmp_path / "out.duckdb"
    report = extract(src, recipe, out)

    chk = _attach_pair(src, out)
    try:
        # 只输出所需集合：订单 10 + 父记录客户 1；无关的 11/12 与客户 2 不在
        assert chk.execute("SELECT count(*) FROM t.main.orders").fetchone()[0] == 1
        assert chk.execute("SELECT count(*) FROM t.main.customers").fetchone()[0] == 1
        # 空表保留结构与类型
        assert chk.execute("SELECT count(*) FROM t.main.audit_log").fetchone()[0] == 0
        desc = {
            d[0]: str(d[1])
            for d in chk.execute("SELECT * FROM t.main.audit_log LIMIT 0").description
        }
        assert desc == {"aid": "INTEGER", "at_ns": "TIMESTAMP_NS"}
        # 补入的父记录同样保真（纳秒 + 间隔成分 + 子->父两表一致）
        assert chk.execute(
            "SELECT epoch_ns(vip_since), datepart('month', note) "
            "FROM t.main.customers WHERE cid = 1"
        ).fetchone() == (1577836800000000001, 3)
        assert chk.execute(
            "SELECT count(*) FROM s.main.customers s "
            "JOIN t.main.customers t USING (cid) "
            "WHERE s.vip_since = t.vip_since "
            "AND datepart('month', s.note) = datepart('month', t.note)"
        ).fetchone()[0] == 1
        assert chk.execute(
            "SELECT count(*) FROM s.main.orders s "
            "JOIN t.main.orders t USING (oid) "
            "WHERE s.placed_at = t.placed_at "
            "AND datepart('month', s.ttl) = datepart('month', t.ttl)"
        ).fetchone()[0] == 1
    finally:
        chk.close()
    # 追查说明中的身份与实际库一致
    by_table = {t["table"]: t for t in report["tables"]}
    assert {tuple(s["identity"]) for s in by_table["main.orders"]["selected"]} == {(10,)}
    assert {tuple(s["identity"]) for s in by_table["main.customers"]["selected"]} == {(1,)}
    assert by_table["main.audit_log"]["selected"] == []
    cust = by_table["main.customers"]["selected"][0]
    assert cust["origin"]["kind"] == "derived"
    assert [p["reference"] for p in cust["origin"]["path"]] == ["orders_customer"]


# ---------------------------------------------------------------------------
# 5) 精确整数、DECIMAL、二进制字段不引入回归
# ---------------------------------------------------------------------------


def test_exact_int_decimal_blob_fidelity(tmp_path):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE vals (id INTEGER, h HUGEINT, u UBIGINT, "
        "price DECIMAL(38,9), amount DECIMAL(10,2), b BLOB)"
    )
    con.execute(
        "INSERT INTO vals VALUES "
        "(1, 170141183460469231731687303715884105727, 18446744073709551615,"
        " 123456789.123456789, 12.34, '\\x00\\x01\\x02\\xff'::BLOB),"
        "(2, -170141183460469231731687303715884105728, 0,"
        " -0.000000001, -99.99, ''::BLOB),"
        "(3, 0, 42, NULL, NULL, NULL)"
    )
    con.close()
    recipe = _write_recipe(tmp_path, {
        "tables": [{"name": "vals", "identity": ["id"]}],
        "seeds": [{"table": "vals", "identity": [1]},
                  {"table": "vals", "identity": [2]},
                  {"table": "vals", "identity": [3]}],
    })
    out = tmp_path / "out.duckdb"
    extract(src, recipe, out)

    chk = _attach_pair(src, out)
    try:
        desc = {
            d[0]: str(d[1])
            for d in chk.execute("SELECT * FROM t.main.vals LIMIT 0").description
        }
        assert desc == {
            "id": "INTEGER", "h": "HUGEINT", "u": "UBIGINT",
            "price": "DECIMAL(38,9)", "amount": "DECIMAL(10,2)", "b": "BLOB",
        }
        # 整数与二进制无损，可直接取回比对
        rows = chk.execute("SELECT id, h, u, b FROM t.main.vals ORDER BY id").fetchall()
        assert rows == [
            (1, 170141183460469231731687303715884105727, 18446744073709551615,
             b"\x00\x01\x02\xff"),
            (2, -170141183460469231731687303715884105728, 0, b""),
            (3, 0, 42, None),
        ]
        # DECIMAL 用数据库内精确等值与文本展开双重核对
        assert chk.execute(
            "SELECT count(*) FROM s.main.vals s JOIN t.main.vals t USING (id) "
            "WHERE s.price IS NOT DISTINCT FROM t.price "
            "AND s.amount IS NOT DISTINCT FROM t.amount"
        ).fetchone()[0] == 3
        assert chk.execute(
            "SELECT price::VARCHAR FROM t.main.vals WHERE id = 1"
        ).fetchone()[0] == "123456789.123456789"
        assert chk.execute(
            "SELECT price::VARCHAR FROM t.main.vals WHERE id = 2"
        ).fetchone()[0] == "-0.000000001"
    finally:
        chk.close()


# ---------------------------------------------------------------------------
# 6) 一致读取：记录选择与数据搬运必须来自同一次一致读取
# ---------------------------------------------------------------------------


def test_selection_and_movement_share_one_consistent_read(tmp_path, monkeypatch):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE events (id INTEGER, occurred TIMESTAMP_NS, term INTERVAL)"
    )
    con.execute(
        "INSERT INTO events VALUES "
        "(1, TIMESTAMP_NS '2026-02-01 12:00:00.123456789', INTERVAL '1 month'),"
        "(2, TIMESTAMP_NS '2026-02-02 00:00:00.000000002', INTERVAL '2 days')"
    )
    con.close()
    recipe = _write_recipe(tmp_path, {
        "tables": [{"name": "events", "identity": ["id"]}],
        "seeds": [{"table": "events", "identity": [1]}],
    })
    # 同一文件的硬链接别名：绕过同进程唯一句柄检查，模拟"另一个写入者"
    alias = tmp_path / "alias.duckdb"
    os.link(src, alias)

    original_compute_closure = engine_mod.compute_closure

    def mutate_source_mid_read(**kwargs):
        """闭包算完（读取事务仍在进行）后，经别名连接改写源库同一行。"""
        closure = original_compute_closure(**kwargs)
        writer = duckdb.connect(str(alias))
        try:
            writer.execute(
                "UPDATE events SET occurred = TIMESTAMP_NS '2030-01-01 00:00:00', "
                "term = INTERVAL '99 years' WHERE id = 1"
            )
        finally:
            writer.close()
        return closure

    monkeypatch.setattr(engine_mod, "compute_closure", mutate_source_mid_read)
    out = tmp_path / "out.duckdb"
    extract(src, recipe, out)

    chk = _attach_pair(src, out)
    try:
        # 注入确实生效：源库磁盘上已是改写后的值
        assert chk.execute(
            "SELECT datepart('year', term) FROM s.main.events WHERE id = 1"
        ).fetchone()[0] == 99
        # 目标库必须是读取事务快照里的原值，而不是改写后的值
        assert chk.execute(
            "SELECT epoch_ns(occurred) FROM t.main.events WHERE id = 1"
        ).fetchone()[0] == 1769947200123456789
        assert chk.execute(
            "SELECT datepart('month', term) FROM t.main.events WHERE id = 1"
        ).fetchone()[0] == 1
        # 未选中的行根本不在目标库
        assert chk.execute("SELECT count(*) FROM t.main.events").fetchone()[0] == 1
    finally:
        chk.close()


class _TxnRecorder:
    """包在真实连接外的记录器：记下事务边界与每条 SQL（含参数）。

    功能测试（上一个用例）证明快照语义；但 DuckDB 对只读挂载有块缓存，
    "结束读取、同连接开新事务再捞" 的模式可能碰巧也读到旧快照——本记录器
    从结构上钉死：所有源库读取与搬运插入必须落在 **同一个** BEGIN..COMMIT
    窗口里，不多一个事务、不中途回滚。
    """

    def __init__(self, real: duckdb.DuckDBPyConnection) -> None:
        self._real = real
        self.calls: list[tuple[str, str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append(("EXECUTE", sql, params))
        if params is None:
            return self._real.execute(sql)
        return self._real.execute(sql, params)

    def begin(self):
        self.calls.append(("BEGIN", "", None))
        return self._real.begin()

    def commit(self):
        self.calls.append(("COMMIT", "", None))
        return self._real.commit()

    def rollback(self):
        self.calls.append(("ROLLBACK", "", None))
        return self._real.rollback()

    def close(self):
        self.calls.append(("CLOSE", "", None))
        return self._real.close()


def test_single_transaction_covers_selection_and_movement(tmp_path, monkeypatch):
    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute(
        "CREATE TABLE events (id INTEGER, occurred TIMESTAMP_NS, term INTERVAL)"
    )
    con.execute(
        "INSERT INTO events VALUES "
        "(1, TIMESTAMP_NS '2026-02-01 12:00:00.123456789', INTERVAL '1 month')"
    )
    con.close()
    recipe = _write_recipe(tmp_path, {
        "tables": [{"name": "events", "identity": ["id"]}],
        "seeds": [{"table": "events", "identity": [1]}],
    })

    recorders: list[_TxnRecorder] = []
    real_open = engine_mod.open_source_session

    def recording_open(path):
        recorder = _TxnRecorder(real_open(path))
        recorders.append(recorder)
        return recorder

    monkeypatch.setattr(engine_mod, "open_source_session", recording_open)
    out = tmp_path / "out.duckdb"
    extract(src, recipe, out)

    assert len(recorders) == 1, "裁取应只建一个源库会话"
    calls = recorders[0].calls
    verbs = [verb for verb, _, _ in calls]
    assert verbs.count("BEGIN") == 1, f"应恰好一个事务，实际 {verbs}"
    assert verbs.count("COMMIT") == 1, f"应恰好一次提交，实际 {verbs}"
    assert "ROLLBACK" not in verbs, f"成功裁取不应回滚，实际 {verbs}"
    lo, hi = verbs.index("BEGIN"), verbs.index("COMMIT")
    # 每条源库读取（SELECT）与搬运插入（INSERT）都在该事务窗口内
    read_or_move = [
        i for i, (verb, sql, _) in enumerate(calls)
        if verb == "EXECUTE" and sql.lstrip().upper().startswith(("SELECT", "INSERT"))
    ]
    assert read_or_move, "应实际发生读取与搬运"
    assert all(lo < i < hi for i in read_or_move), (
        "存在事务窗口外的读取或搬运："
        f"{[(i, calls[i][1][:60]) for i in read_or_move if not lo < i < hi]}"
    )
    # 搬运确实发生了（INSERT 进目标库），且目标内容正确
    assert any(
        verb == "EXECUTE" and sql.lstrip().upper().startswith("INSERT")
        for verb, sql, _ in calls
    )
    chk = _attach_pair(src, out)
    try:
        assert chk.execute(
            "SELECT epoch_ns(occurred) FROM t.main.events WHERE id = 1"
        ).fetchone()[0] == 1769947200123456789
    finally:
        chk.close()


# ---------------------------------------------------------------------------
# 7) 写出中途真实失败：不留半成品/成功说明，恢复后正常，源库不变
# ---------------------------------------------------------------------------


def _build_big_source(src: Path) -> None:
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE big (id INTEGER, payload VARCHAR)")
    # 单行数 MB：搬运写 WAL 时必然突破随后设定的文件大小上限
    con.execute("INSERT INTO big VALUES (1, repeat('z', 4194304))")
    con.close()


def _big_recipe(tmp_path: Path, name: str = "recipe.json") -> Path:
    return _write_recipe(tmp_path, {
        "tables": [{"name": "big", "identity": ["id"]}],
        "seeds": [{"table": "big", "identity": [1]}],
    }, name)


def test_write_failure_midway_leaves_nothing_and_recovers(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_big_source(src)
    recipe = _big_recipe(tmp_path)
    src_hash = _sha256(src)

    # 先成功裁一次，量出完整目标库大小，据此把上限调到必然写不完
    probe_out = tmp_path / "probe.duckdb"
    extract(src, recipe, probe_out)
    full_size = probe_out.stat().st_size
    probe_out.unlink()

    out = tmp_path / "out.duckdb"
    soft_limit = full_size // 2
    prev_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    prev_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        # 只降软上限（硬上限不动才能恢复）；忽略 SIGXFSZ 让写失败变成
        # 真正的 IO 错误而不是杀死进程
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft_limit, prev_limit[1]))
        with pytest.raises(TargetWriteError):
            extract(src, recipe, out)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, prev_limit)
        signal.signal(signal.SIGXFSZ, prev_handler)

    # 目标名下不留半成品，临时文件不留残骸
    assert not out.exists()
    assert not list(tmp_path.glob("out.duckdb.tmp-*"))
    # 修正失败条件后换一个空目标应能正常裁取
    out2 = tmp_path / "out2.duckdb"
    extract(src, recipe, out2)
    chk = _attach_pair(src, out2)
    try:
        assert chk.execute(
            "SELECT (SELECT payload FROM s.main.big WHERE id = 1) "
            "= (SELECT payload FROM t.main.big WHERE id = 1)"
        ).fetchone()[0] is True
    finally:
        chk.close()
    # 源库内容保持不变
    assert _sha256(src) == src_hash


def _child_file_size_limit(soft_limit: int):
    def apply() -> None:
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft_limit, hard))

    return apply


def test_cli_write_failure_exit_code_no_report_then_success(tmp_path):
    src = tmp_path / "src.duckdb"
    _build_big_source(src)
    recipe = _big_recipe(tmp_path)

    probe_out = tmp_path / "probe.duckdb"
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), "extract", "--source", str(src),
         "--recipe", str(recipe), "--target", str(probe_out)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0
    soft_limit = probe_out.stat().st_size // 2
    probe_out.unlink()

    out = tmp_path / "out.duckdb"
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), "extract", "--source", str(src),
         "--recipe", str(recipe), "--target", str(out),
         "--report", str(report)],
        capture_output=True, text=True, cwd=ROOT,
        preexec_fn=_child_file_size_limit(soft_limit),
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "target_write_error"
    # 目标名下不留半成品，也不留成功说明
    assert not out.exists()
    assert not report.exists()
    assert not list(tmp_path.glob("out.duckdb.tmp-*"))

    # 修正失败条件后换一个空目标正常裁取，报告与库一致
    out2 = tmp_path / "out2.duckdb"
    report2 = tmp_path / "report2.json"
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), "extract", "--source", str(src),
         "--recipe", str(recipe), "--target", str(out2),
         "--report", str(report2)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0
    ok_payload = json.loads(proc.stdout)
    assert ok_payload["status"] == "ok"
    assert json.loads(report2.read_text(encoding="utf-8")) == ok_payload
    chk = _attach_pair(src, out2)
    try:
        assert chk.execute(
            "SELECT (SELECT payload FROM s.main.big WHERE id = 1) "
            "= (SELECT payload FROM t.main.big WHERE id = 1)"
        ).fetchone()[0] is True
    finally:
        chk.close()
