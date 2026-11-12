"""传感器视图组发布演示：实际建库、播种、预览、发布、核对业务数值。

场景：传感器测量明细（sensor_readings 基础表）经三层视图逐层计算——

    sensor_clean     清洗（剔除故障与超量程读数）
    sensor_hourly    小时汇总（样本数 / 平均温度 / 最低电量）
    sensor_alerts    告警（高温 / 低电量）

演示两版发布：

  * v1 首次安装（清单故意乱序，执行顺序应由引用关系决定）；
  * v2 上游更名（sensor_clean -> sensor_cleaned，下游同步改）、
    告警阈值 30 -> 31.5、撤下无人引用的 sensor_device_count。

发布前后查询到的业务数值都与下面硬编码的独立预期逐项比对；
任何一步不符即以非零退出码失败。

运行：python3 -I demo/sensor/run_demo.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "src" / "viewctl" / "cli.py"
ASSETS_V1 = ROOT / "demo" / "sensor" / "assets_v1"
ASSETS_V2 = ROOT / "demo" / "sensor" / "assets_v2"

import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

# --- 确定性播种数据：8 行测量明细（1 行 FAULT、1 行超量程，应被清洗剔除） ---
SEED_ROWS = [
    ("s1", "2026-03-01 10:05:00", 21.5, 90.0, "OK"),
    ("s1", "2026-03-01 10:35:00", 22.5, 89.0, "OK"),
    ("s1", "2026-03-01 11:05:00", 23.0, 88.0, "OK"),
    ("s1", "2026-03-01 11:50:00", 24.0, 87.0, "FAULT"),   # 故障：剔除
    ("s2", "2026-03-01 10:10:00", 30.0, 60.0, "OK"),
    ("s2", "2026-03-01 10:40:00", 32.0, 59.0, "OK"),
    ("s2", "2026-03-01 11:15:00", 33.0, 58.0, "OK"),
    ("s2", "2026-03-01 11:45:00", 999.0, 57.0, "OK"),     # 超量程：剔除
]

# --- 独立预期（手工计算，与视图定义无关） ---
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
    ("s2", datetime(2026, 3, 1, 10), "LOW_BATTERY"),  # 31.0 < 31.5，电量 59 < 60
    ("s2", datetime(2026, 3, 1, 11), "HIGH_TEMP"),
]
EXPECT_DEVICE_COUNT_V1 = 2


def run_cli(*args: str) -> dict:
    """通过真实 CLI 入口（子进程）执行并解析 JSON 输出。"""
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(
            f"CLI 输出不是 JSON（退出码 {proc.returncode}）:\n{proc.stdout}\n{proc.stderr}"
        )
    return {"exit": proc.returncode, **payload}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  [OK] {message}")


def seed(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE main.sensor_readings ("
        "sensor_id VARCHAR, measured_at TIMESTAMP, temperature DOUBLE, "
        "battery DOUBLE, status VARCHAR)"
    )
    con.executemany("INSERT INTO main.sensor_readings VALUES (?, ?, ?, ?, ?)", SEED_ROWS)
    con.close()


def query_business_values(db_path: Path, alerts_view: str) -> tuple[list, list]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        hourly = con.execute(
            "SELECT sensor_id, hour_ts, samples, avg_temperature, min_battery "
            "FROM sensor_hourly ORDER BY sensor_id, hour_ts"
        ).fetchall()
        alerts = con.execute(
            f"SELECT sensor_id, hour_ts, alert_kind FROM {alerts_view} "
            "ORDER BY sensor_id, hour_ts"
        ).fetchall()
        return hourly, alerts
    finally:
        con.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="viewctl_demo_"))
    db = tmp / "sensor.duckdb"
    seed(db)
    print(f"演示库文件: {db}（含 sensor_readings {len(SEED_ROWS)} 行）\n")

    print("== 1) v1 预览（清单乱序：alerts/device_count/hourly/clean） ==")
    out = run_cli("preview", "--db", str(db), "--assets", str(ASSETS_V1))
    check(out["exit"] == 0 and out["status"] == "ok", "v1 预览成功")
    order = [op["name"] for op in out["plan"]["order"]]
    check(
        order.index("main.sensor_clean") < order.index("main.sensor_hourly")
        < order.index("main.sensor_alerts"),
        f"执行顺序按引用拓扑而非清单顺序: {order}",
    )
    check(out["db_version"] == 0, "首次发布前登记版本为 0")

    print("\n== 2) v1 应用 ==")
    token = out["token"]
    out = run_cli(
        "apply", "--db", str(db), "--assets", str(ASSETS_V1),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    check(out["exit"] == 0 and out["status"] == "ok", "v1 应用成功")
    check(out["db_version"] == 1, "版本号推进到 1")

    hourly, alerts = query_business_values(db, "sensor_alerts")
    check(hourly == EXPECT_HOURLY, f"v1 小时汇总数值符合独立预期: {hourly}")
    check(alerts == EXPECT_ALERTS_V1, f"v1 告警数值符合独立预期: {alerts}")
    con = duckdb.connect(str(db), read_only=True)
    count = con.execute("SELECT active_sensors FROM sensor_device_count").fetchone()[0]
    con.close()
    check(count == EXPECT_DEVICE_COUNT_V1, f"v1 在线设备数 = {count}")

    print("\n== 3) v2 预览（上游更名 + 阈值调整 + 撤下旧对象） ==")
    out = run_cli("preview", "--db", str(db), "--assets", str(ASSETS_V2))
    check(out["exit"] == 0 and out["status"] == "ok", "v2 预览成功")
    plan = out["plan"]
    check(plan["create"] == ["main.sensor_cleaned"], f"新建: {plan['create']}")
    check(
        plan["replace"] == ["main.sensor_hourly", "main.sensor_alerts"],
        f"替换: {plan['replace']}",
    )
    check(
        plan["drop"] == ["main.sensor_clean", "main.sensor_device_count"],
        f"撤下: {plan['drop']}",
    )
    order = [op["name"] for op in plan["order"]]
    check(
        order.index("main.sensor_cleaned") < order.index("main.sensor_hourly")
        and order.index("main.sensor_clean") > order.index("main.sensor_hourly"),
        f"先建新上游再换下游，最后撤旧对象: {order}",
    )

    print("\n== 4) v2 应用 ==")
    token = out["token"]
    out = run_cli(
        "apply", "--db", str(db), "--assets", str(ASSETS_V2),
        "--expect-db-version", str(token["db_version"]),
        "--expect-source-digest", token["source_digest"],
    )
    check(out["exit"] == 0 and out["status"] == "ok", "v2 应用成功")
    check(out["db_version"] == 2, "版本号推进到 2")

    hourly, alerts = query_business_values(db, "sensor_alerts")
    check(hourly == EXPECT_HOURLY, "v2 小时汇总数值不变（更名不改口径）")
    check(alerts == EXPECT_ALERTS_V2, f"v2 告警阈值生效，符合独立预期: {alerts}")
    con = duckdb.connect(str(db), read_only=True)
    remaining = {
        r[0]
        for r in con.execute(
            "SELECT view_name FROM duckdb_views() "
            "WHERE database_name = current_database()"
        ).fetchall()
    }
    con.close()
    check(
        "sensor_clean" not in remaining and "sensor_device_count" not in remaining,
        "旧对象 sensor_clean / sensor_device_count 已撤下",
    )
    check(
        {"sensor_cleaned", "sensor_hourly", "sensor_alerts"} <= remaining,
        "新三层视图就位",
    )

    print("\n== 5) 重开库文件，登记状态仍可识别 ==")
    out = run_cli("status", "--db", str(db))
    check(out["exit"] == 0 and out["db_version"] == 2, "重开后版本号仍为 2")
    managed = {m["name"] for m in out["managed"]}
    check(
        managed == {"main.sensor_cleaned", "main.sensor_hourly", "main.sensor_alerts"},
        f"重开后管理集正确: {sorted(managed)}",
    )
    out = run_cli("preview", "--db", str(db), "--assets", str(ASSETS_V2))
    check(
        out["status"] == "ok" and out["plan"]["order"] == [] and len(out["plan"]["keep"]) == 3,
        "同候选再次预览：全部保留、无操作（幂等）",
    )

    print("\n演示完成：两版发布均实际落库，业务数值与独立预期一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
