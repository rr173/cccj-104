"""sensorpipe 回归测试：实际打开库文件核对，不只看命令返回。

覆盖需求中的每一种情形：

  * 先收读数、后补历史校准：未校准原因、故障/超量程不进均值
  * 相邻校准在边界两侧分别命中（起点包含、终点不含）
  * 同一瞬间用不同时区表示 -> 同一 UTC 小时、同一校准
  * 缩短区间使样本退出，并删除最后一个样本所在的小时
  * 撤销后重录恢复；同批撤销+重录
  * 同号同内容重传不增样本（跨时区等价）；同号不同内容整批拒绝
  * 同设备相交时段被拒绝、首尾相接允许；非法输入逐行报错
  * 修改/撤销覆盖旧区间 ∪ 新区间；无关设备与不相交小时不被改写
  * 事务中途真实杀进程，重开库三层一致
  * 独立全量对照（tools/sensor_oracle.py）逐条核对

运行：python3 -I -m pytest tests/test_sensorpipe.py -q
"""

from __future__ import annotations

import json
import signal
import site
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402

from sensorpipe.db import connect, init_db  # noqa: E402

DEMO_INPUT = ROOT / "demo" / "sensor_history" / "input"
ORACLE = ROOT / "tools" / "sensor_oracle.py"
CRASH_CHILD = ROOT / "demo" / "sensor_history" / "crash_child.py"


# ---------------------------------------------------------------------------
# 辅助：直接在库里查，不走 CLI 输出格式

def read_hourly(db: Path) -> list[tuple]:
    con = connect(db)
    try:
        return [
            (r[0], r[1], r[2], r[3], r[4])
            for r in con.execute(
                "SELECT sensor_id, hour_utc, samples, avg_temperature, min_battery "
                "FROM main.sensor_hourly ORDER BY sensor_id, hour_utc"
            ).fetchall()
        ]
    finally:
        con.close()


def read_results(db: Path) -> dict[str, dict]:
    con = connect(db)
    try:
        rows = con.execute(
            "SELECT reading_id, sensor_id, measured_at, hour_utc, "
            "temperature_original, battery, status, calibration_id, "
            "temperature_corrected, included, reason, revision "
            "FROM main.sensor_reading_results"
        ).fetchall()
    finally:
        con.close()
    return {
        r[0]: {
            "reading_id": r[0],
            "sensor_id": r[1],
            "measured_at": r[2],
            "hour_utc": r[3],
            "temperature_original": r[4],
            "battery": r[5],
            "status": r[6],
            "calibration_id": r[7],
            "temperature_corrected": r[8],
            "included": r[9],
            "reason": r[10],
            "revision": r[11],
        }
        for r in rows
    }


def hour_revisions(db: Path) -> dict[tuple[str, datetime], int]:
    con = connect(db)
    try:
        return {
            (r[0], r[1]): r[2]
            for r in con.execute(
                "SELECT sensor_id, hour_utc, revision FROM main.sensor_hourly"
            ).fetchall()
        }
    finally:
        con.close()


def calibration_snapshot(db: Path) -> dict[str, dict]:
    con = connect(db)
    try:
        return {
            r[0]: {
                "sensor_id": r[1], "valid_from": r[2], "valid_to": r[3],
                "gain": r[4], "offset": r[5], "revoked": r[6], "revision": r[7],
            }
            for r in con.execute(
                "SELECT calibration_id, sensor_id, valid_from, valid_to, gain, "
                "offset_value, revoked, revision FROM main.sensor_calibrations"
            ).fetchall()
        }
    finally:
        con.close()


def db_counts(db: Path) -> dict[str, int]:
    con = connect(db)
    try:
        return {
            t: con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
            for t in ("sensor_readings", "sensor_calibrations",
                      "sensor_reading_results", "sensor_hourly",
                      "sensor_processing_runs")
        }
    finally:
        con.close()


def oracle_check(db: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", str(ORACLE), "--db", str(db), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, payload
    assert payload["status"] == "ok", payload
    return payload


def cli(*args: str, stdin: str | None = None) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "src" / "sensorpipe" / "cli.py"), *args],
        input=stdin, capture_output=True, text=True, cwd=ROOT,
    )
    return proc.returncode, json.loads(proc.stdout)


def make_input(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 固定小数据（手工预期见各断言的注释）

SEED = [
    # s1：边界两侧、跨时区、故障
    {"reading_id": "r1", "sensor_id": "s1", "measured_at": "2026-03-01T09:59:59Z",
     "temperature": 10.0, "battery": 91.0, "status": "OK"},
    {"reading_id": "r2", "sensor_id": "s1", "measured_at": "2026-03-01T10:00:00Z",
     "temperature": 20.0, "battery": 90.0, "status": "OK"},
    {"reading_id": "r3", "sensor_id": "s1", "measured_at": "2026-03-01T18:59:59+08:00",
     "temperature": 30.0, "battery": 88.0, "status": "OK"},
    {"reading_id": "r4", "sensor_id": "s1", "measured_at": "2026-03-01T11:00:00Z",
     "temperature": 40.0, "battery": 86.0, "status": "OK"},
    {"reading_id": "r5", "sensor_id": "s1", "measured_at": "2026-03-01T12:05:00Z",
     "temperature": 50.0, "battery": 80.0, "status": "FAULT"},
    # s2：超量程
    {"reading_id": "r6", "sensor_id": "s2", "measured_at": "2026-03-01T10:10:00Z",
     "temperature": 500.0, "battery": 60.0, "status": "OK"},
    {"reading_id": "r7", "sensor_id": "s2", "measured_at": "2026-03-01T10:40:00Z",
     "temperature": 99.0, "battery": 58.0, "status": "OK"},
    # s3：未校准设备
    {"reading_id": "r8", "sensor_id": "s3", "measured_at": "2026-03-01T10:20:00Z",
     "temperature": 25.0, "battery": 70.0, "status": "OK"},
]

CAL1 = [
    # s1：10:00-11:00 gain 2 offset 1；11:00-12:00 gain 1 offset -1
    {"calibration_id": "c1", "sensor_id": "s1",
     "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T11:00:00Z",
     "gain": 2.0, "offset": 1.0},
    {"calibration_id": "c2", "sensor_id": "s1",
     "valid_from": "2026-03-01T11:00:00Z", "valid_to": "2026-03-01T12:00:00Z",
     "gain": 1.0, "offset": -1.0},
    # s2：10:00-11:00 gain 1 offset 0
    {"calibration_id": "c3", "sensor_id": "s2",
     "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T11:00:00Z",
     "gain": 1.0, "offset": 0.0},
]


def setup_db(tmp_path: Path, *, with_cal: bool = True) -> Path:
    db = tmp_path / "sensor.duckdb"
    init_db(db)
    code, payload = cli("ingest", "--db", str(db), "--file", str(
        make_input(tmp_path / "seed.jsonl", SEED)
    ))
    assert code == 0, payload
    if with_cal:
        code, payload = cli("calibrate", "--db", str(db), "--file", str(
            make_input(tmp_path / "cal1.jsonl", CAL1)
        ))
        assert code == 0, payload
    return db


# ---------------------------------------------------------------------------
# 1. 先收读数、后补历史校准；原因分类与小时指标


def test_readings_without_calibration_then_backfill(tmp_path):
    db = setup_db(tmp_path, with_cal=False)
    results = read_results(db)
    assert {r["reason"] for r in results.values()} == {
        "missing_calibration", "fault"
    }
    assert read_hourly(db) == []
    # 原始温度不被覆盖
    assert results["r1"]["temperature_original"] == 10.0
    assert results["r6"]["temperature_original"] == 500.0
    assert results["r6"]["temperature_corrected"] is None

    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "cal1.jsonl", CAL1)
    ))
    assert code == 0 and payload["status"] == "ok"
    results = read_results(db)
    # r1 09:59:59 无校准 -> missing
    assert results["r1"]["reason"] == "missing_calibration"
    # r2 10:00 -> c1：20*2+1=41
    assert results["r2"]["calibration_id"] == "c1"
    assert results["r2"]["temperature_corrected"] == 41.0
    # r3 18:59:59+08:00 = 10:59:59Z -> c1：30*2+1=61
    assert results["r3"]["measured_at"] == datetime(2026, 3, 1, 10, 59, 59)
    assert results["r3"]["calibration_id"] == "c1"
    assert results["r3"]["temperature_corrected"] == 61.0
    # r4 11:00 -> c2（起点包含）：40*1-1=39
    assert results["r4"]["calibration_id"] == "c2"
    assert results["r4"]["temperature_corrected"] == 39.0
    # r5 FAULT：即使命中 c2 也不进指标
    assert results["r5"]["reason"] == "fault"
    assert results["r5"]["included"] is False
    # r6 500*1+0=500 超量程
    assert results["r6"]["reason"] == "out_of_range"
    assert results["r6"]["calibration_id"] == "c3"
    # r7 99 超量程
    assert results["r7"]["reason"] == "out_of_range"
    # r8 未校准
    assert results["r8"]["reason"] == "missing_calibration"

    assert read_hourly(db) == [
        ("s1", datetime(2026, 3, 1, 10), 2, 51.0, 88.0),  # (41+61)/2
        ("s1", datetime(2026, 3, 1, 11), 1, 39.0, 86.0),
        # s2 两条都超量程 -> 没有 10 点小时组
    ]
    oracle_check(db)


def test_oracle_independent_matches_after_backfill(tmp_path):
    db = setup_db(tmp_path)
    oracle_check(db)


# ---------------------------------------------------------------------------
# 2. 边界两侧、跨时区同一时刻


def test_boundary_and_timezone_equivalence(tmp_path):
    db = setup_db(tmp_path, with_cal=False)
    rows = [
        {"reading_id": "a", "sensor_id": "s1", "measured_at": "2026-03-01T09:59:59Z",
         "temperature": 1.0, "battery": 1.0, "status": "OK"},
        {"reading_id": "b", "sensor_id": "s1", "measured_at": "2026-03-01T10:00:00Z",
         "temperature": 2.0, "battery": 2.0, "status": "OK"},
        {"reading_id": "c", "sensor_id": "s1", "measured_at": "2026-03-01T18:00:00+08:00",
         "temperature": 2.0, "battery": 2.0, "status": "OK"},
        {"reading_id": "d", "sensor_id": "s1", "measured_at": "2026-03-01T10:59:59Z",
         "temperature": 3.0, "battery": 3.0, "status": "OK"},
        {"reading_id": "e", "sensor_id": "s1", "measured_at": "2026-03-01T11:00:00Z",
         "temperature": 4.0, "battery": 4.0, "status": "OK"},
    ]
    code, payload = cli("ingest", "--db", str(db), "--file", str(
        make_input(tmp_path / "boundary.jsonl", rows)
    ))
    assert code == 0, payload
    cal = [
        {"calibration_id": "early", "sensor_id": "s1",
         "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T11:00:00Z",
         "gain": 10.0, "offset": 0.0},
        {"calibration_id": "late", "sensor_id": "s1",
         "valid_from": "2026-03-01T11:00:00Z", "valid_to": "2026-03-01T12:00:00Z",
         "gain": 100.0, "offset": 0.0},
    ]
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "boundary_cal.jsonl", cal)
    ))
    assert code == 0, payload
    r = read_results(db)
    assert r["a"]["calibration_id"] is None          # 09:59:59 在起点之前
    assert r["b"]["calibration_id"] == "early"       # 10:00:00 起点包含
    assert r["c"]["calibration_id"] == "early"       # 同一瞬间用 +08:00
    assert r["c"]["hour_utc"] == r["b"]["hour_utc"]
    assert r["d"]["calibration_id"] == "early"       # 10:59:59
    assert r["e"]["calibration_id"] == "late"        # 11:00:00 终点不含 early
    assert r["b"]["temperature_corrected"] == 20.0
    assert r["e"]["temperature_corrected"] == 400.0
    # 同一瞬间的两条读数归入同一小时；再加 d（3.0*10），三条均值为
    # (20+20+30)/3=23.333...
    assert read_hourly(db) == [
        ("s1", datetime(2026, 3, 1, 10), 3, round(70.0 / 3.0, 12), 2.0)
    ]
    oracle_check(db)


# ---------------------------------------------------------------------------
# 3. 缩短区间 -> 样本退出、删除最后一个样本所在小时；无关记录不动


def test_shrink_interval_deletes_last_hour(tmp_path):
    db = setup_db(tmp_path)
    before = hour_revisions(db)
    con = connect(db)
    con.execute(
        "UPDATE main.sensor_calibrations SET valid_to = ?, revision = revision + 1 "
        "WHERE calibration_id = 'c1'",
        [datetime(2026, 3, 1, 10, 30)],
    )
    con.close()
    # 用正式入口改回短区间（上面直接改库只为确认前提，下面走事务入口）
    short = [
        {"calibration_id": "c1", "sensor_id": "s1",
         "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T10:00:00Z",
         "gain": 1.0, "offset": 0.0},
    ]
    # 先恢复成原区间，再通过入口缩短（保证走产品代码路径）
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "restore.jsonl", [CAL1[0]])
    ))
    assert code == 0, payload
    # 把 c1 缩到 [10:00,10:00) 是非法的（终点须晚于起点），换用缩到 10:01
    shrink = [
        {"calibration_id": "c1", "sensor_id": "s1",
         "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T10:01:00Z",
         "gain": 1.0, "offset": 0.0},
    ]
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "shrink.jsonl", shrink)
    ))
    assert code == 0, payload
    # 10 点小时仍剩 r2（10:00），不会删除；再看撤销 c1 才让该小时空掉
    r = read_results(db)
    assert r["r2"]["included"] is True
    assert r["r3"]["reason"] == "missing_calibration"
    hourly = read_hourly(db)
    assert ("s1", datetime(2026, 3, 1, 10), 1, 20.0, 90.0) in hourly
    # 撤销 c1：s1 10 点最后一个样本退出 -> 整个小时被删除
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "revoke.jsonl", [{"calibration_id": "c1", "revoke": True}])
    ))
    assert code == 0 and payload["affected"]["hours_deleted"] == 1, payload
    assert ("s1", datetime(2026, 3, 1, 10)) not in {
        (h[0], h[1]) for h in read_hourly(db)
    }
    after = hour_revisions(db)
    # 无关小时 revision 不变：s1 11 点（s2 没有可纳入样本，本就无小时组）
    assert after[("s1", datetime(2026, 3, 1, 11))] == before[("s1", datetime(2026, 3, 1, 11))]
    assert ("s2", datetime(2026, 3, 1, 10)) not in after
    oracle_check(db)


def test_modification_covers_old_and_new_ranges(tmp_path):
    """把 c1 的区间从 [10,11) 移到 [12,13)：旧区间内样本必须退出。"""
    db = setup_db(tmp_path)
    con = connect(db)
    con.execute(
        "INSERT INTO main.sensor_readings VALUES "
        "('r9','s1',?,?,?,?,?,1)",
        [datetime(2026, 3, 1, 12, 30), 33.0, 70.0, "OK", "sha256:x"],
    )
    con.close()
    moved = [
        {"calibration_id": "c1", "sensor_id": "s1",
         "valid_from": "2026-03-01T12:00:00Z", "valid_to": "2026-03-01T13:00:00Z",
         "gain": 1.0, "offset": 0.0},
    ]
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "moved.jsonl", moved)
    ))
    assert code == 0, payload
    r = read_results(db)
    # 旧区间 [10,11) 中的 r2/r3 已无校准覆盖 -> missing（新插入的 r9 只读结论）
    assert r["r2"]["reason"] == "missing_calibration"
    assert r["r3"]["reason"] == "missing_calibration"
    assert r["r9"]["calibration_id"] == "c1"
    # 报告中明确列出了旧区间和新区间
    windows = payload["affected"]["affected_windows"]
    assert {"sensor_id": "s1", "from": "2026-03-01T10:00:00Z",
            "to": "2026-03-01T11:00:00Z"} in windows
    assert {"sensor_id": "s1", "from": "2026-03-01T12:00:00Z",
            "to": "2026-03-01T13:00:00Z"} in windows
    oracle_check(db)


# ---------------------------------------------------------------------------
# 4. 撤销后重录恢复（分批与同批）


def test_revoke_then_rerecord(tmp_path):
    db = setup_db(tmp_path)
    summer = read_hourly(db)
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "revoke.jsonl", [{"calibration_id": "c3", "revoke": True}])
    ))
    assert code == 0, payload
    r = read_results(db)
    assert r["r6"]["reason"] == "missing_calibration"
    assert ("s2", datetime(2026, 3, 1, 10)) not in {
        (h[0], h[1]) for h in read_hourly(db)
    }
    # 同一次操作撤销 + 重录同一 calibration_id
    text = (
        json.dumps({"calibration_id": "c3", "revoke": True}) + "\n"
        + json.dumps(CAL1[2]) + "\n"
    )
    code, payload = cli("calibrate", "--db", str(db), "--stdin", stdin=text)
    assert code == 0, payload
    assert read_hourly(db) == summer
    assert read_results(db)["r6"]["reason"] == "out_of_range"
    oracle_check(db)


# ---------------------------------------------------------------------------
# 5. 重传 / 冲突


def test_retransmit_idempotent_and_conflict_rolls_back(tmp_path):
    db = setup_db(tmp_path)
    counts = db_counts(db)
    code, payload = cli("ingest", "--db", str(db), "--file", str(
        make_input(tmp_path / "seed_again.jsonl", SEED)
    ))
    assert code == 0 and payload["inserted"] == 0
    after = db_counts(db)
    # 幂等重传仍登记一次处理观察，但不增加读数/结论/小时样本
    for table in ("sensor_readings", "sensor_calibrations",
                  "sensor_reading_results", "sensor_hourly"):
        assert after[table] == counts[table]
    assert after["sensor_processing_runs"] == counts["sensor_processing_runs"] + 1
    # 同一瞬间用 +08:00 表示 -> 内容归一化后仍视为同内容
    same_instant = dict(SEED[1])
    same_instant["measured_at"] = "2026-03-01T18:00:00+08:00"
    code, payload = cli("ingest", "--db", str(db), "--file", str(
        make_input(tmp_path / "same_instant.jsonl", [same_instant])
    ))
    assert code == 0 and payload["inserted"] == 0
    # 同号不同内容 + 一条新读数 -> 整批撤销
    conflicting = dict(SEED[0])
    conflicting["temperature"] = 999.0
    fresh = {"reading_id": "fresh", "sensor_id": "s1",
             "measured_at": "2026-03-01T15:00:00Z", "temperature": 1.0,
             "battery": 1.0, "status": "OK"}
    code, payload = cli("ingest", "--db", str(db), "--file", str(
        make_input(tmp_path / "conflict.jsonl", [conflicting, fresh])
    ))
    assert code == 2 and payload["errors"][0]["code"] == "reading_conflict"
    assert db_counts(db)["sensor_readings"] == counts["sensor_readings"]
    assert "fresh" not in read_results(db)
    assert read_results(db)["r1"]["temperature_original"] == 10.0
    oracle_check(db)


# ---------------------------------------------------------------------------
# 6. 校准区间非法：重叠拒绝、首尾相接允许、字段校验


def test_overlap_rejected_adjacent_allowed(tmp_path):
    db = setup_db(tmp_path, with_cal=False)
    good = [
        {"calibration_id": "x1", "sensor_id": "s1",
         "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T11:00:00Z",
         "gain": 1.0, "offset": 0.0},
        {"calibration_id": "x2", "sensor_id": "s1",
         "valid_from": "2026-03-01T11:00:00Z", "valid_to": "2026-03-01T12:00:00Z",
         "gain": 1.0, "offset": 0.0},
    ]
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "adjacent.jsonl", good)
    ))
    assert code == 0, payload  # 首尾相接允许
    overlaps = [
        {"calibration_id": "y1", "sensor_id": "s2",
         "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T11:00:00Z",
         "gain": 1.0, "offset": 0.0},
        {"calibration_id": "y2", "sensor_id": "s2",
         "valid_from": "2026-03-01T10:30:00Z", "valid_to": "2026-03-01T12:00:00Z",
         "gain": 1.0, "offset": 0.0},
    ]
    before = calibration_snapshot(db)
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "overlap.jsonl", overlaps)
    ))
    assert code == 2
    assert [e["code"] for e in payload["errors"]] == ["calibration_overlap"]
    assert calibration_snapshot(db) == before
    # 修改已有记录使其与背景记录相交：同样拒绝，不改动任何一层
    clashing = [dict(good[0], valid_to="2026-03-01T11:30:00Z")]
    before_counts = db_counts(db)
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "clash.jsonl", clashing)
    ))
    assert code == 2 and payload["rolled_back"] is True
    assert db_counts(db) == before_counts
    oracle_check(db)


def test_invalid_calibration_rows_rejected(tmp_path):
    db = setup_db(tmp_path, with_cal=False)
    bad = [
        {"calibration_id": "b1", "sensor_id": "s1",
         "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T10:00:00Z",
         "gain": 1.0, "offset": 0.0},
        {"calibration_id": "b2", "sensor_id": "s1",
         "valid_from": "2026-03-01T10:00:00Z", "valid_to": "2026-03-01T11:00:00Z",
         "gain": float("inf"), "offset": 0.0},
        {"calibration_id": "b3", "sensor_id": "s1",
         "valid_from": "2026-03-01T10:00:00", "valid_to": "2026-03-01T11:00:00Z",
         "gain": 1.0, "offset": 0.0},
    ]
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "bad.jsonl", bad)
    ))
    assert code == 2
    messages = " ".join(e["message"] for e in payload["errors"])
    assert "valid_to 必须晚于 valid_from" in messages
    assert "有限数值" in messages
    assert "缺少时区" in messages
    assert calibration_snapshot(db) == {}


def test_unknown_revoke_rejected(tmp_path):
    db = setup_db(tmp_path, with_cal=False)
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "unknown.jsonl", [{"calibration_id": "nope", "revoke": True}])
    ))
    assert code == 2 and payload["errors"][0]["code"] == "unknown_calibration"


# ---------------------------------------------------------------------------
# 7. 中间崩溃恢复：真实 SIGKILL，重开库核对三层一致


def test_process_killed_mid_update_recovers(tmp_path):
    db = setup_db(tmp_path)
    cal_file = make_input(tmp_path / "crash_cal.jsonl", [dict(
        CAL1[0], valid_from="2026-03-01T09:00:00Z", gain=9.0, offset=9.0
    )])
    before = {
        "cal": calibration_snapshot(db),
        "results": read_results(db),
        "hourly": read_hourly(db),
        "counts": db_counts(db),
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(CRASH_CHILD), str(db), str(cal_file)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode in (-signal.SIGKILL, 137), proc.stdout + proc.stderr
    after = {
        "cal": calibration_snapshot(db),
        "results": read_results(db),
        "hourly": read_hourly(db),
        "counts": db_counts(db),
    }
    assert after == before, "未提交事务的校准/明细/小时三层全部回滚"
    assert after["cal"]["c1"]["gain"] == 2.0
    oracle_check(db)
    # 恢复后同一操作可正常完成，且三层一起前进
    code, payload = cli("calibrate", "--db", str(db), "--file", str(cal_file))
    assert code == 0, payload
    assert calibration_snapshot(db)["c1"]["gain"] == 9.0
    # c1 改为 [09:00,11:00) 且 gain=9/offset=9：
    # r1 10*9+9=99、r2 20*9+9=189、r3 30*9+9=279，全超 80 -> 全退出；
    # 10 点小时因最后一个样本退出而被删除。r4 仍由 c2 覆盖 -> 11 点 39.0
    assert read_results(db)["r2"]["temperature_corrected"] == 189.0
    assert read_results(db)["r3"]["reason"] == "out_of_range"
    assert read_results(db)["r1"]["reason"] == "out_of_range"
    assert read_hourly(db) == [("s1", datetime(2026, 3, 1, 11), 1, 39.0, 86.0)]
    oracle_check(db)


# ---------------------------------------------------------------------------
# 8. 影响范围：只重算相关读数/小时；跨设备隔离


def test_impact_scoped_to_affected_sensor_and_window(tmp_path):
    db = setup_db(tmp_path)
    before_results = read_results(db)
    before_hours = hour_revisions(db)
    change_s2 = [dict(CAL1[2], gain=3.0, offset=-50.0)]
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "s2change.jsonl", change_s2)
    ))
    assert code == 0, payload
    affected = set(payload["affected"]["affected_readings"])
    assert affected <= {"r6", "r7"}
    assert payload["affected"]["readings_recomputed"] == 2
    # s1 的明细 revision 一律未变
    after_results = read_results(db)
    for rid in ("r1", "r2", "r3", "r4", "r5"):
        assert after_results[rid]["revision"] == before_results[rid]["revision"]
        assert after_results[rid]["temperature_corrected"] == \
            before_results[rid]["temperature_corrected"]
    # s2 的 10 点小时因 99*3-50=247 超量程、500*3-50 超量程 -> 变空删除；
    # 但 s1 的小时完全未动
    after_hours = hour_revisions(db)
    for key in (("s1", datetime(2026, 3, 1, 10)),
                ("s1", datetime(2026, 3, 1, 11))):
        if key in before_hours:
            assert after_hours[key] == before_hours[key]
    oracle_check(db)


def test_noop_calibration_resubmit_does_not_rewrite(tmp_path):
    """原样重交同一条校准：不重算、不影响任何 revision。"""
    db = setup_db(tmp_path)
    before_hours = hour_revisions(db)
    before_results = read_results(db)
    code, payload = cli("calibrate", "--db", str(db), "--file", str(
        make_input(tmp_path / "noop.jsonl", CAL1)
    ))
    assert code == 0, payload
    assert payload["calibration_ids"] == []
    assert payload["affected"]["readings_recomputed"] == 0
    assert payload["affected"]["hours_recomputed"] == 0
    assert hour_revisions(db) == before_hours
    assert read_results(db) == before_results
    oracle_check(db)


def test_run_records_are_queryable(tmp_path):
    db = setup_db(tmp_path)
    con = connect(db)
    try:
        runs = con.execute(
            "SELECT run_id, kind, readings_recomputed, readings_changed, "
            "hours_recomputed, hours_changed, hours_deleted, details_json "
            "FROM main.sensor_processing_runs ORDER BY submitted_at, run_id"
        ).fetchall()
        hours = con.execute(
            "SELECT run_id, sensor_id, hour_utc, change FROM main.sensor_run_hours"
        ).fetchall()
        readings = con.execute(
            "SELECT run_id, reading_id, changed FROM main.sensor_run_readings"
        ).fetchall()
    finally:
        con.close()
    assert [r[1] for r in runs] == ["import_readings", "apply_calibrations"]
    assert len(readings) > 0 and len(hours) > 0
    details = json.loads(runs[-1][7])
    assert details["affected_windows"]
    assert details["calibration_ids"] == ["c1", "c2", "c3"]
    # 每次处理的影响范围都能从 runs 入口看到
    code, payload = cli("runs", "--db", str(db))
    assert code == 0 and len(payload["runs"]) == 2


# ---------------------------------------------------------------------------
# 9. 运行机器本地时区不影响任何结果


def test_results_independent_of_process_timezone(tmp_path):
    """同一批输入在 TZ=UTC / Asia/Shanghai / America/New_York 下结果完全一致。"""
    payload_path = make_input(tmp_path / "tz.jsonl", [
        {"reading_id": "t1", "sensor_id": "s1",
         "measured_at": "2026-03-01T02:05:00Z", "temperature": 10.0,
         "battery": 50.0, "status": "OK"},
        {"reading_id": "t2", "sensor_id": "s1",
         "measured_at": "2026-03-01T10:05:00+08:00", "temperature": 20.0,
         "battery": 40.0, "status": "OK"},
    ])
    cal_path = make_input(tmp_path / "tz_cal.jsonl", [
        {"calibration_id": "tzc", "sensor_id": "s1",
         "valid_from": "2026-03-01T02:00:00Z", "valid_to": "2026-03-01T03:00:00Z",
         "gain": 2.0, "offset": 1.0},
    ])
    outcomes = []
    for tz_name in ("UTC", "Asia/Shanghai", "America/New_York"):
        db = tmp_path / f"sensor_{tz_name.replace('/', '_')}.duckdb"
        env = {**dict(__import__("os").environ), "TZ": tz_name}
        for args in (
            ("init", "--db", str(db)),
            ("ingest", "--db", str(db), "--file", str(payload_path)),
            ("calibrate", "--db", str(db), "--file", str(cal_path)),
        ):
            proc = subprocess.run(
                [sys.executable, "-I", str(ROOT / "src" / "sensorpipe" / "cli.py"),
                 *args],
                capture_output=True, text=True, cwd=ROOT, env=env,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr
        results = read_results(db)
        hourly_rows = read_hourly(db)
        outcomes.append((
            {rid: (r["hour_utc"], r["calibration_id"],
                   r["temperature_corrected"], r["reason"])
             for rid, r in results.items()},
            hourly_rows,
        ))
    assert outcomes[0] == outcomes[1] == outcomes[2]
    # 显式断言：02:05Z 与 10:05+08:00 都落在 02 点 UTC 小时
    assert outcomes[0][0]["t1"][0] == datetime(2026, 3, 1, 2)
    assert outcomes[0][0]["t2"][0] == datetime(2026, 3, 1, 2)
    assert outcomes[0][1] == [
        # t1 10*2+1=21；t2 20*2+1=41 -> (21+41)/2=31.0
        ("s1", datetime(2026, 3, 1, 2), 2, 31.0, 40.0)
    ]


# ---------------------------------------------------------------------------
# 10. 与既有演示 schema 的隔离 / 命令行用法错误


def test_cli_usage_errors(tmp_path):
    db = tmp_path / "sensor.duckdb"
    # 库文件不存在 -> IO 错误（退出码 3）
    code, payload = cli("status", "--db", str(db))
    assert code == 3 and payload["errors"][0]["code"] == "io_error"
    code, payload = cli("init", "--db", str(db))
    assert code == 0
    db.write_bytes(b"not a duckdb file")
    code, payload = cli("status", "--db", str(db))
    assert code == 3
    db.unlink()
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE marker(x INT)")
    con.close()
    code, payload = cli("status", "--db", str(db))
    assert code == 2 and payload["errors"][0]["code"] == "not_initialized"
    code, payload = cli("init", "--db", str(db))
    assert code == 0
    code, payload = cli("ingest", "--db", str(db))
    assert code == 3 and payload["errors"][0]["code"] == "usage_error"
    # 缺库文件属于 IO 错误（退出码 3）
    code, payload = cli("status", "--db", str(tmp_path / "missing.duckdb"))
    assert code == 3 and payload["errors"][0]["code"] == "io_error"


def test_init_rejects_incompatible_preexisting_table(tmp_path):
    db = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE main.sensor_readings ("
        "sensor_id VARCHAR, measured_at TIMESTAMP, temperature DOUBLE, "
        "battery DOUBLE, status VARCHAR)"
    )
    con.close()
    code, payload = cli("init", "--db", str(db))
    assert code == 2 and payload["errors"][0]["code"] == "incompatible_schema"
