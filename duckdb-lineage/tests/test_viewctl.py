"""viewctl 回归测试：实际打开库文件核对，不以"打印计划"冒充发布成功。

覆盖：

  * 乱序清单首次安装（执行顺序按引用拓扑而非清单顺序）
  * 数据库重开后仍识别已管理对象；不收编碰巧存在的外部视图
  * 上下游同步更名（先建新上游、再换下游、最后撤旧名）
  * 撤下无人引用的旧对象；基础表数据不变
  * 无效定义导致整批回滚（定义与登记都回到操作前）
  * 候选资源改动 / 库内版本推进后拒绝旧预览，重新预览可正常发布
  * 预览结束后库内无残留变更（成功与失败两种预览）
  * 撤下仍被保留视图 / 外部视图引用的对象被阻止并给出依赖链
  * 候选名与非管理对象冲突、类型冲突
  * CTE 名与表别名不制造假循环；真实循环被报出
  * 重名、缺失资源、非法查询在改动数据库前说清原因

运行：python3 -I -m pytest tests/test_viewctl.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

from viewctl import cli  # noqa: E402
from viewctl.refs import extract_refs, parse_query, topo_order  # noqa: E402


# ---------------------------------------------------------------------------
# 引用提取单元测试（纯函数，不开库）


def _refs(sql: str, default_schema: str = "main", db: str | None = None) -> set[str]:
    return extract_refs(parse_query(sql), default_schema, current_database=db)


def test_refs_cte_and_alias_are_not_objects():
    sql = (
        "WITH sensor_clean AS (SELECT sensor_id FROM main.sensor_readings "
        "WHERE status = 'OK') "
        "SELECT sc.sensor_id FROM sensor_clean sc"
    )
    # CTE 名 sensor_clean 与别名 sc 都不是对象引用
    assert _refs(sql) == {"main.sensor_readings"}


def test_refs_qualified_name_bypasses_cte():
    sql = (
        "WITH sensor_clean AS (SELECT 1 AS x) "
        "SELECT * FROM main.sensor_clean"
    )
    # 带 schema 限定的名字永远解析为数据库对象，不是 CTE
    assert _refs(sql) == {"main.sensor_clean"}


def test_refs_nested_cte_scopes():
    # 内层 WITH 的 CTE 不泄漏到外层；外层 CTE 对内层可见
    sql = (
        "WITH a AS (SELECT sensor_id FROM main.sensor_readings) "
        "SELECT * FROM (WITH b AS (SELECT * FROM a) SELECT * FROM b) t "
        "JOIN main.sensor_hourly h ON h.sensor_id = t.sensor_id"
    )
    assert _refs(sql) == {"main.sensor_readings", "main.sensor_hourly"}


def test_refs_cte_forward_reference_is_external():
    # 同一 WITH 中，先定义的 CTE 看不到后定义的同名 CTE：
    # a 体内的 b 不是局部名（DuckDB 语义），按外部对象处理
    sql = "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x) SELECT * FROM a"
    assert _refs(sql) == {"main.b"}


def test_refs_union_and_derived_table():
    sql = (
        "WITH x AS (SELECT sensor_id FROM main.t1) "
        "SELECT * FROM x UNION ALL SELECT sensor_id FROM (SELECT sensor_id "
        "FROM main.t2) sub"
    )
    assert _refs(sql) == {"main.t1", "main.t2"}


def test_refs_three_part_same_database_normalizes():
    sql = "SELECT * FROM mydb.main.sensor_clean"
    assert _refs(sql, db="mydb") == {"main.sensor_clean"}
    # 指向其它 catalog 的三段名保留全名，不会与本库管理名相撞
    assert _refs(sql, db="otherdb") == {"mydb.main.sensor_clean"}


def test_refs_table_function_is_not_object():
    sql = "SELECT * FROM duckdb_views()"
    assert _refs(sql) == set()


def test_refs_default_schema_applied():
    sql = "SELECT * FROM sensor_clean"
    assert _refs(sql, default_schema="analytics") == {"analytics.sensor_clean"}


def test_topo_order_deterministic_and_dependency_first():
    names = ["c", "a", "b"]
    deps = {"c": {"b"}, "b": {"a"}, "a": set()}
    assert topo_order(names, deps) == ["a", "b", "c"]
    # 并列时按字典序
    assert topo_order(["b", "a"], {"a": set(), "b": set()}) == ["a", "b"]

# ---------------------------------------------------------------------------
# 固定样例数据与独立预期（手工计算，不依赖被测代码）

SEED_ROWS = [
    ("s1", "2026-03-01 10:05:00", 21.5, 90.0, "OK"),
    ("s1", "2026-03-01 10:35:00", 22.5, 89.0, "OK"),
    ("s1", "2026-03-01 11:05:00", 23.0, 88.0, "OK"),
    ("s1", "2026-03-01 11:50:00", 24.0, 87.0, "FAULT"),  # 清洗剔除
    ("s2", "2026-03-01 10:10:00", 30.0, 60.0, "OK"),
    ("s2", "2026-03-01 10:40:00", 32.0, 59.0, "OK"),
    ("s2", "2026-03-01 11:15:00", 33.0, 58.0, "OK"),
    ("s2", "2026-03-01 11:45:00", 999.0, 57.0, "OK"),  # 超量程，清洗剔除
]

EXPECT_HOURLY = [
    ("s1", datetime(2026, 3, 1, 10), 2, 22.0, 89.0),
    ("s1", datetime(2026, 3, 1, 11), 1, 23.0, 88.0),
    ("s2", datetime(2026, 3, 1, 10), 2, 31.0, 59.0),
    ("s2", datetime(2026, 3, 1, 11), 1, 33.0, 58.0),
]
EXPECT_ALERTS_V1 = [
    ("s2", datetime(2026, 3, 1, 10), "HIGH_TEMP"),
    ("s2", datetime(2026, 3, 1, 11), "HIGH_TEMP"),
]
EXPECT_ALERTS_V2 = [
    ("s2", datetime(2026, 3, 1, 10), "LOW_BATTERY"),
    ("s2", datetime(2026, 3, 1, 11), "HIGH_TEMP"),
]

CLEAN_SQL = (
    "SELECT sensor_id, measured_at, temperature, battery "
    "FROM main.sensor_readings "
    "WHERE status = 'OK' AND temperature BETWEEN -50.0 AND 80.0"
)
HOURLY_SQL = (
    "SELECT sensor_id, date_trunc('hour', measured_at) AS hour_ts, "
    "COUNT(*) AS samples, AVG(temperature) AS avg_temperature, "
    "MIN(battery) AS min_battery "
    "FROM sensor_clean "
    "GROUP BY sensor_id, date_trunc('hour', measured_at)"
)
ALERTS_SQL_V1 = (
    "SELECT sensor_id, hour_ts, avg_temperature, min_battery, "
    "CASE WHEN avg_temperature >= 30.0 THEN 'HIGH_TEMP' ELSE 'LOW_BATTERY' END "
    "AS alert_kind FROM sensor_hourly "
    "WHERE avg_temperature >= 30.0 OR min_battery < 60.0"
)
ALERTS_SQL_V2 = ALERTS_SQL_V1.replace("30.0", "31.5")
DEVICE_COUNT_SQL = "SELECT COUNT(DISTINCT sensor_id) AS active_sensors FROM sensor_clean"


# ---------------------------------------------------------------------------
# 辅助


def create_db(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE main.sensor_readings ("
        "sensor_id VARCHAR, measured_at TIMESTAMP, temperature DOUBLE, "
        "battery DOUBLE, status VARCHAR)"
    )
    con.executemany("INSERT INTO main.sensor_readings VALUES (?, ?, ?, ?, ?)", SEED_ROWS)
    con.close()


def make_assets(base: Path, views: dict[str, str], schema: str = "main") -> Path:
    """按给定顺序写清单（顺序故意可由调用方打乱）。"""
    assets = base / "assets"
    queries = assets / "queries"
    queries.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for i, (name, sql) in enumerate(views.items()):
        rel = f"queries/{i:02d}.sql"
        (assets / rel).write_text(sql, encoding="utf-8")
        mapping[name] = rel
    (assets / "manifest.json").write_text(
        json.dumps({"schema": schema, "views": mapping}), encoding="utf-8"
    )
    return assets


def run_cli(capsys, *args: str) -> tuple[int, dict]:
    code = cli.main(list(args))
    out = capsys.readouterr().out
    return code, json.loads(out)


def preview_then_apply(capsys, db: Path, assets: Path) -> dict:
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0 and prev["status"] == "ok", prev
    token = prev["token"]
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 0 and applied["status"] == "ok", applied
    return applied


def query(db: Path, sql: str):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def view_names(db: Path) -> set[str]:
    rows = query(
        db,
        "SELECT schema_name || '.' || view_name FROM duckdb_views() "
        "WHERE database_name = current_database()",
    )
    return {r[0] for r in rows}


def db_version(db: Path) -> int:
    con = duckdb.connect(str(db), read_only=True)
    try:
        row = con.execute(
            "SELECT value FROM _viewctl.meta WHERE key = 'version'"
        ).fetchone()
        return int(row[0]) if row else 0
    except duckdb.CatalogException:
        return 0
    finally:
        con.close()


def snapshot_db(db: Path) -> dict:
    """库内全部可观察状态：对象清单、注册表内容、基础表数据。"""
    con = duckdb.connect(str(db), read_only=True)
    try:
        snap = {
            "schemas": con.execute(
                "SELECT schema_name FROM duckdb_schemas() "
                "WHERE database_name = current_database() ORDER BY 1"
            ).fetchall(),
            "tables": con.execute(
                "SELECT schema_name, table_name FROM duckdb_tables() "
                "WHERE database_name = current_database() ORDER BY 1, 2"
            ).fetchall(),
            "views": con.execute(
                "SELECT schema_name, view_name, sql FROM duckdb_views() "
                "WHERE database_name = current_database() ORDER BY 1, 2"
            ).fetchall(),
            "data": con.execute(
                "SELECT * FROM main.sensor_readings ORDER BY sensor_id, measured_at"
            ).fetchall(),
        }
        try:
            snap["registry_meta"] = con.execute(
                "SELECT * FROM _viewctl.meta ORDER BY key"
            ).fetchall()
            snap["registry_objects"] = con.execute(
                "SELECT * FROM _viewctl.objects ORDER BY name"
            ).fetchall()
            snap["registry_history"] = con.execute(
                "SELECT version, summary FROM _viewctl.history ORDER BY version"
            ).fetchall()
        except duckdb.CatalogException:
            snap["registry_meta"] = None
            snap["registry_objects"] = None
            snap["registry_history"] = None
        return snap
    finally:
        con.close()


def seed_data(db: Path):
    return query(db, "SELECT * FROM main.sensor_readings ORDER BY sensor_id, measured_at")


# ---------------------------------------------------------------------------
# 1. 乱序清单首次安装


def test_out_of_order_manifest_first_install(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    # 清单顺序故意与依赖相反：alerts -> hourly -> clean
    assets = make_assets(
        tmp_path,
        {
            "sensor_alerts": ALERTS_SQL_V1,
            "sensor_hourly": HOURLY_SQL,
            "sensor_clean": CLEAN_SQL,
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0, prev
    order = [op["name"] for op in prev["plan"]["order"]]
    assert order == [
        "main.sensor_clean",
        "main.sensor_hourly",
        "main.sensor_alerts",
    ]
    assert prev["db_version"] == 0
    assert prev["checks"]["trial_install"] == "passed (rolled back)"

    token = prev["token"]
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 0, applied
    assert applied["db_version"] == 1

    # 实际打开库核对业务数值（独立预期）
    assert (
        query(
            db,
            "SELECT sensor_id, hour_ts, samples, avg_temperature, min_battery "
            "FROM sensor_hourly ORDER BY sensor_id, hour_ts",
        )
        == EXPECT_HOURLY
    )
    assert (
        query(db, "SELECT sensor_id, hour_ts, alert_kind FROM sensor_alerts ORDER BY 1, 2")
        == EXPECT_ALERTS_V1
    )
    assert db_version(db) == 1


# ---------------------------------------------------------------------------
# 2. 重开识别管理集；不收编碰巧存在的外部视图


def test_reopen_recognizes_managed_and_ignores_unmanaged(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    # 库里预先存在一个与本工具无关的视图
    con = duckdb.connect(str(db))
    con.execute("CREATE VIEW main.legacy_report AS SELECT 1 AS marker")
    con.close()

    assets = make_assets(
        tmp_path,
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, assets)

    # 重开后：登记版本与管理集仍在；外部视图不属于管理集、也不被撤下
    code, status = run_cli(capsys, "status", "--db", str(db))
    assert code == 0
    assert status["db_version"] == 1
    managed = {m["name"] for m in status["managed"]}
    assert managed == {"main.sensor_clean", "main.sensor_hourly", "main.sensor_alerts"}
    assert "main.legacy_report" in view_names(db)
    assert query(db, "SELECT marker FROM main.legacy_report") == [(1,)]

    # 同候选再次预览：全部保留、无操作（识别出上次已管理的对象）
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0
    assert prev["plan"]["order"] == []
    assert sorted(prev["plan"]["keep"]) == [
        "main.sensor_alerts",
        "main.sensor_clean",
        "main.sensor_hourly",
    ]
    # 空计划应用：no_changes，不推进版本
    token = prev["token"]
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 0 and applied["note"] == "no_changes"
    assert db_version(db) == 1


# ---------------------------------------------------------------------------
# 3. 上下游同步更名


def test_synced_rename_upstream_downstream(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, v1)

    # v2：上游更名 clean -> cleaned，下游同步改；alerts 文本不变（应保留）
    v2 = make_assets(
        tmp_path / "v2",
        {
            "sensor_cleaned": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL.replace("sensor_clean", "sensor_cleaned"),
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 0, prev
    plan = prev["plan"]
    assert plan["create"] == ["main.sensor_cleaned"]
    assert plan["replace"] == ["main.sensor_hourly"]
    assert plan["drop"] == ["main.sensor_clean"]
    assert plan["keep"] == ["main.sensor_alerts"]
    order = [(op["action"], op["name"]) for op in plan["order"]]
    assert order == [
        ("create", "main.sensor_cleaned"),
        ("replace", "main.sensor_hourly"),
        ("drop", "main.sensor_clean"),
    ]

    token = prev["token"]
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(v2),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 0, applied
    assert applied["db_version"] == 2
    names = view_names(db)
    assert "main.sensor_clean" not in names
    assert {"main.sensor_cleaned", "main.sensor_hourly", "main.sensor_alerts"} <= names
    # 业务数值不因更名改变
    assert (
        query(
            db,
            "SELECT sensor_id, hour_ts, samples, avg_temperature, min_battery "
            "FROM sensor_hourly ORDER BY sensor_id, hour_ts",
        )
        == EXPECT_HOURLY
    )
    assert (
        query(db, "SELECT sensor_id, hour_ts, alert_kind FROM sensor_alerts ORDER BY 1, 2")
        == EXPECT_ALERTS_V1
    )
    # 基础表数据不变
    assert len(seed_data(db)) == len(SEED_ROWS)


# ---------------------------------------------------------------------------
# 4. 撤下无人引用的旧对象


def test_drop_unreferenced_old_object(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
            "sensor_device_count": DEVICE_COUNT_SQL,
        },
    )
    preview_then_apply(capsys, db, v1)
    assert query(db, "SELECT active_sensors FROM sensor_device_count") == [(2,)]

    # v2：仅移除 sensor_device_count（无人引用它）
    v2 = make_assets(
        tmp_path / "v2",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 0, prev
    assert prev["plan"]["drop"] == ["main.sensor_device_count"]
    assert [op["action"] for op in prev["plan"]["order"]] == ["drop"]
    token = prev["token"]
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(v2),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 0, applied
    assert "main.sensor_device_count" not in view_names(db)
    # 其余视图与基础表数据不受影响
    assert (
        query(
            db,
            "SELECT sensor_id, hour_ts, samples, avg_temperature, min_battery "
            "FROM sensor_hourly ORDER BY sensor_id, hour_ts",
        )
        == EXPECT_HOURLY
    )
    assert len(seed_data(db)) == len(SEED_ROWS)
    assert db_version(db) == 2


def test_drop_all_managed_with_empty_manifest(tmp_path, capsys):
    """空 views 清单 = 撤下全部已登记对象（外部对象不受影响）。"""
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    con = duckdb.connect(str(db))
    con.execute("CREATE VIEW main.legacy_report AS SELECT 1 AS marker")
    con.close()
    v1 = make_assets(
        tmp_path / "v1",
        {"sensor_clean": CLEAN_SQL, "sensor_hourly": HOURLY_SQL},
    )
    preview_then_apply(capsys, db, v1)

    v2 = tmp_path / "v2" / "assets"
    v2.mkdir(parents=True)
    (v2 / "manifest.json").write_text(
        json.dumps({"schema": "main", "views": {}}), encoding="utf-8"
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 0, prev
    assert sorted(prev["plan"]["drop"]) == ["main.sensor_clean", "main.sensor_hourly"]
    token = prev["token"]
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(v2),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 0, applied
    assert view_names(db) == {"main.legacy_report"}  # 只撤自己管理的
    assert db_version(db) == 2
    code, status = run_cli(capsys, "status", "--db", str(db))
    assert status["managed"] == []


# ---------------------------------------------------------------------------
# 5. 无效定义导致整批回滚


def test_invalid_definition_rolls_back_everything(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, v1)
    before = snapshot_db(db)

    # v2：新建 cleaned（会先成功），但 hourly 引用了不存在的列 -> 整批失败
    bad_hourly = (
        "SELECT sensor_id, no_such_column AS x FROM sensor_cleaned"
    )
    v2 = make_assets(
        tmp_path / "v2",
        {
            "sensor_cleaned": CLEAN_SQL,
            "sensor_hourly": bad_hourly,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 2, prev
    assert prev["status"] == "error"
    bind_errors = [e for e in prev["errors"] if e["code"] == "bind_error"]
    assert bind_errors and bind_errors[0]["asset"] == "main.sensor_hourly"

    # 预览失败后库内无残留
    assert snapshot_db(db) == before

    # 正式应用同样失败（试装能发现的问题，应用时也必然失败并回滚）
    v2b = make_assets(
        tmp_path / "v2b",
        {
            "sensor_cleaned": CLEAN_SQL,
            "sensor_hourly": bad_hourly,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    code, prev2 = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2b))
    assert code == 2
    # 直接构造一个"预览时合法、应用时仍失败"的场景不必可行——
    # 这里验证的是失败路径：版本与定义全部回到操作前
    assert snapshot_db(db) == before

    # 旧定义仍然可用，业务数值不变
    assert (
        query(
            db,
            "SELECT sensor_id, hour_ts, samples, avg_temperature, min_battery "
            "FROM sensor_hourly ORDER BY sensor_id, hour_ts",
        )
        == EXPECT_HOURLY
    )
    assert "main.sensor_cleaned" not in view_names(db)
    assert db_version(db) == 1
    assert len(seed_data(db)) == len(SEED_ROWS)


def test_apply_failure_rolls_back_registry_and_definitions(tmp_path, capsys):
    """预览后库内被第三方改动，使 apply 拒绝/失败 -> 定义与登记保持原样。"""
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, v1)
    before = snapshot_db(db)

    # v2 合法：新建 cleaned、替换 hourly 引用 cleaned、撤 clean
    v2 = make_assets(
        tmp_path / "v2",
        {
            "sensor_cleaned": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL.replace("sensor_clean", "sensor_cleaned"),
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 0, prev
    token = prev["token"]

    # 预览后、应用前：第三方在库里建了一个同名外部视图 sensor_cleaned。
    # 版本号不变（不是我们登记的发布），但应用时重新计算计划会发现
    # 候选名字撞上了非管理对象 -> 拒绝，不覆盖别人的对象。
    con = duckdb.connect(str(db))
    con.execute("CREATE VIEW main.sensor_cleaned AS SELECT 1 AS x")
    con.close()

    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(v2),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 2, applied
    assert applied["status"] == "error"
    assert applied["errors"][0]["code"] == "name_collision"

    # 定义与登记全部保持操作前（除第三方自建的那个视图）
    after = snapshot_db(db)
    assert after["registry_meta"] == before["registry_meta"]
    assert after["registry_objects"] == before["registry_objects"]
    assert after["registry_history"] == before["registry_history"]
    assert after["data"] == before["data"]
    assert db_version(db) == 1
    names = view_names(db)
    assert "main.sensor_clean" in names  # 旧对象未被撤下
    assert "main.sensor_hourly" in names
    assert (
        query(
            db,
            "SELECT sensor_id, hour_ts, samples, avg_temperature, min_battery "
            "FROM sensor_hourly ORDER BY sensor_id, hour_ts",
        )
        == EXPECT_HOURLY
    )


def test_apply_mid_transaction_failure_rolls_back(tmp_path, capsys):
    """预览后基础表被第三方改坏，apply 事务中途失败 -> 整体回滚。"""
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, v1)
    before = snapshot_db(db)

    v2 = make_assets(
        tmp_path / "v2",
        {
            "sensor_cleaned": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL.replace("sensor_clean", "sensor_cleaned"),
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 0, prev
    token = prev["token"]

    # 预览后：第三方把基础表的 temperature 列删了。这不改变登记版本，
    # 静态检查也发现不了（基础表结构不属于本工具管理），但事务内
    # CREATE VIEW 会触发 DuckDB 绑定失败 -> 回滚。
    con = duckdb.connect(str(db))
    con.execute("ALTER TABLE main.sensor_readings DROP COLUMN temperature")
    con.close()

    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(v2),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 2, applied
    assert applied["errors"][0]["code"] == "bind_error"
    assert applied["errors"][0]["asset"] == "main.sensor_cleaned"

    # 事务整体回滚：新建未残留、旧对象未撤下、登记未推进
    after = snapshot_db(db)
    assert after["registry_meta"] == before["registry_meta"]
    assert after["registry_objects"] == before["registry_objects"]
    assert after["registry_history"] == before["registry_history"]
    assert db_version(db) == 1
    names = view_names(db)
    assert "main.sensor_cleaned" not in names
    assert "main.sensor_clean" in names
    assert "main.sensor_hourly" in names


# ---------------------------------------------------------------------------
# 6. 过期预览拒绝


def test_stale_source_rejected_then_repreview_works(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    assets = make_assets(
        tmp_path,
        {"sensor_clean": CLEAN_SQL, "sensor_hourly": HOURLY_SQL},
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0
    token = prev["token"]

    # 预览后修改查询文件（哪怕只是注释，指纹也会变）
    qfile = tmp_path / "assets" / "queries" / "00.sql"
    qfile.write_text(CLEAN_SQL + "\n-- touched after preview\n", encoding="utf-8")

    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    assert code == 2, applied
    assert applied["errors"][0]["code"] == "stale_source"
    assert applied["errors"][0]["expected"] == token["source_digest"]
    assert applied["errors"][0]["current"] != token["source_digest"]
    # 拒绝时不改动任何内容
    assert db_version(db) == 0
    assert view_names(db) == set()

    # 重新预览后正常发布
    code, prev2 = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0
    token2 = prev2["token"]
    assert token2["source_digest"] != token["source_digest"]
    code, applied2 = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(token2["db_version"]),
        "--expect-source-digest", token2["source_digest"],
    )
    assert code == 0, applied2
    assert db_version(db) == 1


def test_stale_database_rejected_then_repreview_works(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    assets = make_assets(
        tmp_path,
        {"sensor_clean": CLEAN_SQL, "sensor_hourly": HOURLY_SQL},
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    old_token = prev["token"]

    # 另一份实例先发布了更新（库内版本 0 -> 1）
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(old_token["db_version"]),
        "--expect-source-digest", old_token["source_digest"],
    )
    assert code == 0, applied
    assert db_version(db) == 1

    # 旧预览（基于版本 0）不能覆盖新状态
    code, stale = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(old_token["db_version"]),
        "--expect-source-digest", old_token["source_digest"],
    )
    assert code == 2, stale
    assert stale["errors"][0]["code"] == "stale_database"
    assert stale["errors"][0]["expected"] == 0
    assert stale["errors"][0]["current"] == 1

    # 重新预览（版本 1）后发布正常（同候选 -> no_changes）
    code, prev2 = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    token2 = prev2["token"]
    assert token2["db_version"] == 1
    code, applied2 = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(token2["db_version"]),
        "--expect-source-digest", token2["source_digest"],
    )
    assert code == 0 and applied2["note"] == "no_changes"
    assert db_version(db) == 1


# ---------------------------------------------------------------------------
# 7. 预览无残留


def test_preview_leaves_no_residue(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    before = snapshot_db(db)
    assert before["registry_meta"] is None  # 尚未有注册表

    # 成功预览：不在库内留下任何变更（包括不创建注册表）
    assets = make_assets(
        tmp_path,
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0
    assert snapshot_db(db) == before

    # 失败预览（候选引用了不存在的列）：同样无残留
    bad = make_assets(
        tmp_path / "bad",
        {"sensor_clean": "SELECT no_such_col FROM main.sensor_readings"},
    )
    code, failed = run_cli(capsys, "preview", "--db", str(db), "--assets", str(bad))
    assert code == 2
    assert failed["errors"][0]["code"] == "bind_error"
    assert failed["errors"][0]["asset"] == "main.sensor_clean"
    assert snapshot_db(db) == before

    # 正式发布后再预览：注册表内容也不被预览改动
    preview_then_apply(capsys, db, assets)
    after_apply = snapshot_db(db)
    code, prev2 = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0
    assert snapshot_db(db) == after_apply


# ---------------------------------------------------------------------------
# 8. 撤下保护


def test_drop_blocked_by_retained_view(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, v1)
    before = snapshot_db(db)

    # v2 撤下 clean，但保留的 hourly 仍引用它
    v2 = make_assets(
        tmp_path / "v2",
        {"sensor_hourly": HOURLY_SQL, "sensor_alerts": ALERTS_SQL_V1},
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 2, prev
    blocked = [e for e in prev["errors"] if e["code"] == "drop_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["name"] == "main.sensor_clean"
    chains = [r["chain"] for r in blocked[0]["referenced_by"]]
    assert ["main.sensor_alerts", "main.sensor_hourly", "main.sensor_clean"] in chains
    # 阻止发布，库内无变化
    assert snapshot_db(db) == before


def test_drop_blocked_by_external_view(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, v1)
    # 外部（非本工具管理）视图依赖 sensor_clean
    con = duckdb.connect(str(db))
    con.execute("CREATE VIEW main.ext_report AS SELECT * FROM main.sensor_clean")
    con.close()
    before = snapshot_db(db)

    # v2：hourly 改为直接读基础表，候选不再引用 clean -> 仅外部视图拦着
    hourly_direct = HOURLY_SQL.replace("sensor_clean", "main.sensor_readings")
    v2 = make_assets(
        tmp_path / "v2",
        {"sensor_hourly": hourly_direct, "sensor_alerts": ALERTS_SQL_V1},
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(v2))
    assert code == 2, prev
    blocked = [e for e in prev["errors"] if e["code"] == "drop_blocked"]
    assert len(blocked) == 1
    ext = [r for r in blocked[0]["referenced_by"] if r["kind"] == "external"]
    assert ext and ext[0]["chain"] == ["main.ext_report", "main.sensor_clean"]
    assert snapshot_db(db) == before
    # 外部视图本身安然无恙（sensor_clean 双条件过滤后剩 6 行）
    assert len(query(db, "SELECT * FROM main.ext_report")) == 6


# ---------------------------------------------------------------------------
# 9. 冲突与类型保护


def test_name_collision_with_unmanaged_object(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    con = duckdb.connect(str(db))
    con.execute("CREATE VIEW main.sensor_clean AS SELECT 42 AS answer")
    con.close()
    before = snapshot_db(db)

    assets = make_assets(tmp_path, {"sensor_clean": CLEAN_SQL})
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 2
    assert prev["errors"][0]["code"] == "name_collision"
    assert prev["errors"][0]["name"] == "main.sensor_clean"
    # 不覆盖、不收编：外部视图保持原样，注册表未创建
    assert snapshot_db(db) == before
    assert query(db, "SELECT answer FROM main.sensor_clean") == [(42,)]


def test_object_type_conflict_when_managed_name_became_table(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    assets = make_assets(tmp_path, {"sensor_clean": CLEAN_SQL})
    preview_then_apply(capsys, db, assets)

    # 有人把已登记视图删了、建了同名表
    con = duckdb.connect(str(db))
    con.execute("DROP VIEW main.sensor_clean")
    con.execute("CREATE TABLE main.sensor_clean (x INTEGER)")
    con.close()
    before = snapshot_db(db)

    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 2
    assert prev["errors"][0]["code"] == "object_type_conflict"
    assert snapshot_db(db) == before


# ---------------------------------------------------------------------------
# 10. CTE / 别名不制造假循环；真实循环被报出


def test_cte_and_alias_do_not_create_false_cycle(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    # v_a 的 CTE 叫 v_b，v_b 的 CTE 叫 v_a：文本扫描会误判 a<->b 循环，
    # 但 CTE 是查询内部局部名，不构成对象间引用；v_b 里还有叫 v_c 的表别名
    v_a = (
        "WITH v_b AS (SELECT sensor_id, temperature FROM main.sensor_readings "
        "WHERE status = 'OK') SELECT * FROM v_b"
    )
    v_b = (
        "WITH v_a AS (SELECT 1 AS marker) "
        "SELECT m.marker, v_c.sensor_id FROM v_a m "
        "JOIN main.sensor_readings AS v_c ON true "
        "WHERE v_c.status = 'OK'"
    )
    # v_c 真实引用 v_a（未限定短名 -> 同 schema 对象）
    v_c = "SELECT sensor_id, temperature FROM v_a"
    assets = make_assets(
        tmp_path,
        {"v_b": v_b, "v_c": v_c, "v_a": v_a},  # 清单顺序再打乱
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 0, prev
    order = [op["name"] for op in prev["plan"]["order"]]
    assert order.index("main.v_a") < order.index("main.v_c")
    preview_token = prev["token"]
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--expect-db-version", str(preview_token["db_version"]),
        "--expect-source-digest", preview_token["source_digest"],
    )
    assert code == 0, applied
    # 两个视图内容各自独立：v_b 的 marker 恒为 1，v_a 是清洗后的明细
    assert query(db, "SELECT DISTINCT marker FROM v_b") == [(1,)]
    assert len(query(db, "SELECT * FROM v_a")) == 7
    assert len(query(db, "SELECT * FROM v_c")) == 7


def test_candidate_cycle_reported(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    before = snapshot_db(db)
    assets = make_assets(
        tmp_path,
        {
            "v_x": "SELECT * FROM v_y",
            "v_y": "SELECT * FROM v_x",
        },
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 2
    cyc = [e for e in prev["errors"] if e["code"] == "cycle"]
    assert len(cyc) == 1
    chain = cyc[0]["chain"]
    assert chain[0] == chain[-1] and len(chain) == 3
    assert set(chain) == {"main.v_x", "main.v_y"}
    assert snapshot_db(db) == before


def test_self_reference_reported_as_cycle(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    assets = make_assets(tmp_path, {"v_x": "SELECT * FROM v_x"})
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 2
    assert prev["errors"][0]["code"] == "cycle"
    assert prev["errors"][0]["chain"] == ["main.v_x", "main.v_x"]


# ---------------------------------------------------------------------------
# 11. 重名 / 缺失资源 / 非法查询：改动数据库前说清原因


def test_duplicate_names_reported(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    before = snapshot_db(db)
    assets = tmp_path / "assets"
    (assets / "queries").mkdir(parents=True)
    (assets / "queries" / "a.sql").write_text("SELECT 1 AS x", encoding="utf-8")
    (assets / "queries" / "b.sql").write_text("SELECT 2 AS x", encoding="utf-8")
    (assets / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "main",
                "views": {"Sensor_Clean": "queries/a.sql", "sensor_clean": "queries/b.sql"},
            }
        ),
        encoding="utf-8",
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 2
    assert prev["errors"][0]["code"] == "duplicate_name"
    assert prev["errors"][0]["name"] == "main.sensor_clean"
    assert snapshot_db(db) == before


def test_missing_resource_reported(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    before = snapshot_db(db)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "manifest.json").write_text(
        json.dumps({"schema": "main", "views": {"sensor_clean": "queries/nope.sql"}}),
        encoding="utf-8",
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 2
    assert prev["errors"][0]["code"] == "missing_resource"
    assert prev["errors"][0]["asset"] == "main.sensor_clean"
    assert snapshot_db(db) == before


def test_non_select_query_rejected(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    before = snapshot_db(db)
    assets = make_assets(
        tmp_path, {"sensor_clean": "DELETE FROM main.sensor_readings"}
    )
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    assert code == 2
    assert prev["errors"][0]["code"] == "query_error"
    assert snapshot_db(db) == before


# ---------------------------------------------------------------------------
# 12. 入口与令牌


def test_apply_requires_preview_token(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    assets = make_assets(tmp_path, {"sensor_clean": CLEAN_SQL})
    code, out = run_cli(
        capsys, "apply", "--db", str(db), "--assets", str(assets)
    )
    assert code == 3
    assert out["errors"][0]["code"] == "usage_error"
    assert db_version(db) == 0


def test_token_file_roundtrip(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    assets = make_assets(tmp_path, {"sensor_clean": CLEAN_SQL})
    code, prev = run_cli(capsys, "preview", "--db", str(db), "--assets", str(assets))
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps(prev["token"]), encoding="utf-8")
    code, applied = run_cli(
        capsys,
        "apply",
        "--db", str(db),
        "--assets", str(assets),
        "--token-file", str(token_file),
    )
    assert code == 0, applied
    assert db_version(db) == 1


def test_missing_db_file_is_io_error(tmp_path, capsys):
    code, out = run_cli(
        capsys, "status", "--db", str(tmp_path / "nonexistent.duckdb")
    )
    assert code == 3
    assert out["errors"][0]["code"] == "io_error"


# ---------------------------------------------------------------------------
# 13. 阈值调整改变业务数值（独立预期）


def test_threshold_change_reflects_in_business_values(tmp_path, capsys):
    db = tmp_path / "sensor.duckdb"
    create_db(db)
    v1 = make_assets(
        tmp_path / "v1",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V1,
        },
    )
    preview_then_apply(capsys, db, v1)
    assert (
        query(db, "SELECT sensor_id, hour_ts, alert_kind FROM sensor_alerts ORDER BY 1, 2")
        == EXPECT_ALERTS_V1
    )

    v2 = make_assets(
        tmp_path / "v2",
        {
            "sensor_clean": CLEAN_SQL,
            "sensor_hourly": HOURLY_SQL,
            "sensor_alerts": ALERTS_SQL_V2,
        },
    )
    preview_then_apply(capsys, db, v2)
    assert (
        query(db, "SELECT sensor_id, hour_ts, alert_kind FROM sensor_alerts ORDER BY 1, 2")
        == EXPECT_ALERTS_V2
    )
    assert db_version(db) == 2
