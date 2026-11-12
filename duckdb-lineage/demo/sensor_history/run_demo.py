"""传感器历史读数补算链——可重跑的小数据演示。

场景（全部通过真实 CLI 子进程执行）：

1. 先收 11 条读数（无校准）：OK 但未命中校准 -> missing_calibration，
   FAULT -> fault，均不进小时指标；小时报表为空。
2. 补 2026-03-01 的历史校准（s1 两段首尾相接、s2 一段）：重算受影响读数与
   小时；不同时区（Z / +08:00）表示的同一时刻落到同一个 UTC 小时。
3. 边界两侧分别命中相邻系数：10:59:59（含起点、不含终点）用 early，
   11:00:00（含起点）用 late。
4. 缩短 s1-early 到 10:00 之前：原先有效的 10 点小时样本全部退出，
   最后一个样本所在的小时被删除；s2 的 10 点小时保持不变（revision 不涨）。
5. 撤销 s1-mid：10 点小时再次消失；同一次操作里撤销并重录同一
   calibration_id -> 恢复。
6. 重传同一份读数（含用 +08:00 表示的同一时刻）不增加样本；
   同 reading_id 不同内容被拒绝并整批撤销；重叠校准被拒绝且整批回滚。
7. 用独立全量对照（tools/sensor_oracle.py）逐条核对三层状态。
8. 在结果更新中途真实 SIGKILL 进程，重开库核对三层一起恢复；
   再正常执行同一操作，确认链路可继续。

运行：python3 -I demo/sensor_history/run_demo.py
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "src" / "sensorpipe" / "cli.py"
ORACLE = ROOT / "tools" / "sensor_oracle.py"
INPUT = ROOT / "demo" / "sensor_history" / "input"
CRASH_CHILD = ROOT / "demo" / "sensor_history" / "crash_child.py"

import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

# ---------------------------------------------------------------------------
# 手工独立预期：全部由纸笔算出，不复用被测视图/函数
#
# 读数（s1-r3 用 +08:00 表示 2026-03-01T10:10:00Z，与 s1-r2 同一瞬间）
# ---------------------------------------------------------------------------

EXPECT_REASONS_AFTER_INGEST = {
    "s1-r1": "missing_calibration",  # 09:50，无校准
    "s1-r2": "missing_calibration",  # 10:10
    "s1-r3": "missing_calibration",  # 10:10（+08:00 换算）
    "s1-r4": "missing_calibration",  # 10:59:59
    "s1-r5": "missing_calibration",  # 11:00:00
    "s1-r6": "missing_calibration",  # 11:30
    "s1-r7": "fault",                # FAULT
    "s2-r1": "missing_calibration",  # 10:05（尚未有校准）
    "s2-r2": "missing_calibration",  # 10:35
    "s2-r3": "missing_calibration",  # 11:15
    "s3-r1": "missing_calibration",  # 10:20，演示"未校准设备"
}

# 第 2 步之后（s1-early=[09:00,11:00) 1.1/+0.5；s1-late=[11:00,13:00) 1.2/-1.0；
# s2-early=[10:00,11:00) 1.0/+0.0）
EXPECT_HOURLY_AFTER_CAL1 = [
    ("s1", "2026-03-01T09:00:00Z", 1, 22.5, 91.0),
    # 10 点：22.0*1.1+0.5=24.7；24*1.1+0.5=26.9；26*1.1+0.5=29.1 -> 均值 26.9
    ("s1", "2026-03-01T10:00:00Z", 3, 26.9, 87.0),
    # 11 点：28*1.2-1=32.6；30*1.2-1=35.0 -> 均值 33.8（FAULT 不进）
    ("s1", "2026-03-01T11:00:00Z", 2, 33.8, 85.0),
    # s2 10 点：500 超量程剔除，只留 31.0
    ("s2", "2026-03-01T10:00:00Z", 1, 31.0, 59.0),
]

EXPECT_REASONS_AFTER_CAL1 = {
    "s1-r1": "included",
    "s1-r2": "included",
    "s1-r3": "included",
    "s1-r4": "included",
    "s1-r5": "included",
    "s1-r6": "included",
    "s1-r7": "fault",
    "s2-r1": "out_of_range",
    "s2-r2": "included",
    "s2-r3": "missing_calibration",
    "s3-r1": "missing_calibration",
}
EXPECT_CORRECTED_AFTER_CAL1 = {
    "s1-r1": 22.5, "s1-r2": 24.7, "s1-r3": 26.9, "s1-r4": 29.1, "s1-r5": 32.6,
    "s1-r6": 35.0, "s1-r7": 7.4, "s2-r1": 500.0, "s2-r2": 31.0,
    "s2-r3": None, "s3-r1": None,
}

# 第 4 步：s1-early 缩到 [09:00,10:00)，s2-late=[11:00,12:00) 1.0/-10.0 补上
EXPECT_HOURLY_AFTER_CAL2 = [
    ("s1", "2026-03-01T09:00:00Z", 1, 22.5, 91.0),
    ("s1", "2026-03-01T11:00:00Z", 2, 33.8, 85.0),
    ("s2", "2026-03-01T10:00:00Z", 1, 31.0, 59.0),
    # s2 11 点：35-10=25.0
    ("s2", "2026-03-01T11:00:00Z", 1, 25.0, 58.0),
]
EXPECT_DELETED_HOURS_CAL2 = {("s1", "2026-03-01T10:00:00Z")}

# 第 5 步：补 s1-mid=[10,11) 1.0/+0.0
EXPECT_HOURLY_AFTER_CAL3 = [
    ("s1", "2026-03-01T09:00:00Z", 1, 22.5, 91.0),
    # 22.0 + 24.0 + 26.0 -> 24.0
    ("s1", "2026-03-01T10:00:00Z", 3, 24.0, 87.0),
    ("s1", "2026-03-01T11:00:00Z", 2, 33.8, 85.0),
    ("s2", "2026-03-01T10:00:00Z", 1, 31.0, 59.0),
    ("s2", "2026-03-01T11:00:00Z", 1, 25.0, 58.0),
]
# 第 6 步撤销 s1-mid：10 点小时被删除；第 7 步撤销并重录后恢复
EXPECT_HOURLY_AFTER_REVOKE = [
    ("s1", "2026-03-01T09:00:00Z", 1, 22.5, 91.0),
    ("s1", "2026-03-01T11:00:00Z", 2, 33.8, 85.0),
    ("s2", "2026-03-01T10:00:00Z", 1, 31.0, 59.0),
    ("s2", "2026-03-01T11:00:00Z", 1, 25.0, 58.0),
]


def run_cli(*args: str, stdin_text: str | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), *args],
        input=stdin_text,
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


def hourly(db: Path) -> list[tuple]:
    payload = run_cli("hourly", "--db", str(db))
    return [
        (
            row["sensor_id"], row["hour_utc"], row["samples"],
            row["avg_temperature"], row["min_battery"],
        )
        for row in payload["hourly"]
    ]


def reasons(db: Path) -> dict[str, str]:
    payload = run_cli("detail", "--db", str(db))
    return {row["reading_id"]: row["reason"] for row in payload["readings"]}


def corrected(db: Path) -> dict[str, float | None]:
    payload = run_cli("detail", "--db", str(db))
    return {
        row["reading_id"]: row["temperature_corrected"] for row in payload["readings"]
    }


def revision_map(db: Path) -> dict[tuple[str, str], int]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {
            (r[0], r[1].strftime("%Y-%m-%dT%H:%M:%SZ")): r[2]
            for r in con.execute(
                "SELECT sensor_id, hour_utc, revision FROM main.sensor_hourly"
            ).fetchall()
        }
    finally:
        con.close()


def run_oracle(db: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", str(ORACLE), "--db", str(db), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    payload = json.loads(proc.stdout)
    check(proc.returncode == 0 and payload["status"] == "ok",
          "独立全量对照与库内三层一致")
    return payload


def three_layers(db: Path) -> dict:
    """直接读原始文件，核对"校准/明细/小时"三层同时是旧值或同时是新值。"""
    con = duckdb.connect(str(db), read_only=True)
    try:
        cal = con.execute(
            "SELECT calibration_id, valid_from, valid_to, gain, offset_value, revoked "
            "FROM main.sensor_calibrations WHERE calibration_id = 's1-early'"
        ).fetchall()
        detail = con.execute(
            "SELECT reading_id, calibration_id, temperature_corrected, included, reason "
            "FROM main.sensor_reading_results "
            "WHERE reading_id IN ('s1-r1','s1-r2','s1-r3','s1-r4') ORDER BY reading_id"
        ).fetchall()
        hours = con.execute(
            "SELECT sensor_id, hour_utc, samples, avg_temperature, min_battery "
            "FROM main.sensor_hourly ORDER BY sensor_id, hour_utc"
        ).fetchall()
        runs = con.execute(
            "SELECT run_id, kind FROM main.sensor_processing_runs "
            "ORDER BY submitted_at, run_id"
        ).fetchall()
        return {"cal": cal, "detail": detail, "hours": hours, "runs": runs}
    finally:
        con.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sensor_history_demo_"))
    db = tmp / "sensor.duckdb"
    out = run_cli("init", "--db", str(db))
    check(out["exit"] == 0 and out["status"] == "ok", "初始化库文件")

    print("== 1) 先收读数（尚无校准） ==")
    out = run_cli("ingest", "--db", str(db), "--file", str(INPUT / "readings.jsonl"))
    check(out["exit"] == 0 and out["status"] == "ok", "读数导入成功")
    check(out["inserted"] == 11, f"入库 11 条读数（实际 {out['inserted']}）")
    check(reasons(db) == EXPECT_REASONS_AFTER_INGEST,
          "未校准设备/未覆盖时段 -> missing_calibration，FAULT -> fault")
    check(hourly(db) == [], "没有任何纳入样本时小时报表为空（不留空组）")

    print("\n== 2) 补历史校准：跨时区同一时刻 + 边界两侧命中相邻系数 ==")
    before_rev = revision_map(db)
    out = run_cli("calibrate", "--db", str(db), "--file", str(INPUT / "calibrations_step1.jsonl"))
    check(out["exit"] == 0, "历史校准补录成功")
    check(out["affected"]["affected_windows"] == [
        {"sensor_id": "s1", "from": "2026-03-01T09:00:00Z", "to": "2026-03-01T13:00:00Z"},
        {"sensor_id": "s2", "from": "2026-03-01T10:00:00Z", "to": "2026-03-01T11:00:00Z"},
    ], f"影响范围仅限被校准覆盖的设备/时段: {out['affected']['affected_windows']}")
    check(hourly(db) == EXPECT_HOURLY_AFTER_CAL1, "小时指标符合手工计算")
    check(reasons(db) == EXPECT_REASONS_AFTER_CAL1, "明细原因符合预期（含超量程/故障）")
    check(corrected(db) == EXPECT_CORRECTED_AFTER_CAL1, "修正温度符合手工计算")
    detail = run_cli("detail", "--db", str(db))["readings"]
    hit = {r["reading_id"]: r["calibration_id"] for r in detail}
    check(hit["s1-r4"] == "s1-early", "10:59:59 命中 early（终点不含）")
    check(hit["s1-r5"] == "s1-late", "11:00:00 命中 late（起点包含）")
    check(hit["s1-r2"] == hit["s1-r3"], "不同时区表示的同一时刻命中同一校准/小时")
    # 未受校准影响的设备 s3 小时组不存在，一级未校准读数保持原判
    check(revision_map(db) != before_rev, "受影响小时被重新计算并提升 revision")
    run_oracle(db)

    print("\n== 3) 缩短区间：样本退出 + 删掉最后一个样本所在的小时 ==")
    before = revision_map(db)
    out = run_cli("calibrate", "--db", str(db), "--file", str(INPUT / "calibrations_step2.jsonl"))
    check(out["exit"] == 0 and out["affected"]["hours_deleted"] == 1,
          "缩短 s1-early 后删除 1 个空小时")
    check(hourly(db) == EXPECT_HOURLY_AFTER_CAL2, "10 点小时被删除，11 点与 s2 保留")
    deleted = {
        (h["sensor_id"], h["hour_utc"])
        for h in out["affected"]["affected_hours"] if h["change"] == "deleted"
    }
    check(deleted == EXPECT_DELETED_HOURS_CAL2, f"删除的正是 {deleted}")
    # 无关设备/未相交时段既有结果不变：s1 09 点、s2 10 点 revision 不涨
    after = revision_map(db)
    check(after[("s1", "2026-03-01T09:00:00Z")] == before[("s1", "2026-03-01T09:00:00Z")],
          "s1 09 点既有时段未被改写")
    check(after[("s2", "2026-03-01T10:00:00Z")] == before[("s2", "2026-03-01T10:00:00Z")],
          "s2 无关设备未被改写")
    check(out["affected"]["readings_recomputed"] < 11,
          f"只重算受影响读数（{out['affected']['readings_recomputed']} < 11）")
    run_oracle(db)

    print("\n== 4) 补入过去的一段（s1-mid）：原未校准样本变为有效 ==")
    out = run_cli("calibrate", "--db", str(db), "--file", str(INPUT / "calibrations_step3.jsonl"))
    check(out["exit"] == 0, "补入 s1-mid 成功")
    check(hourly(db) == EXPECT_HOURLY_AFTER_CAL3, "10 点小时恢复且均值为 24.0")
    # 10 点小时是新建，revision 从 1 开始；11 点和 s2 不应被改写
    after3 = revision_map(db)
    check(after3[("s1", "2026-03-01T11:00:00Z")] == before[("s1", "2026-03-01T11:00:00Z")],
          "s1 11 点仍未被无关校准改写")
    run_oracle(db)

    print("\n== 5) 撤销 -> 删掉空小时；同批撤销并重录 -> 恢复 ==")
    out = run_cli("calibrate", "--db", str(db), "--file", str(INPUT / "revoke_s1_mid.jsonl"))
    check(out["exit"] == 0 and out["affected"]["hours_deleted"] == 1,
          "撤销 s1-mid 删除 10 点小时")
    check(hourly(db) == EXPECT_HOURLY_AFTER_REVOKE, "撤销后报表不含 s1 10 点")
    out = run_cli(
        "calibrate", "--db", str(db),
        "--file", str(INPUT / "revoke_s1_mid_and_recover.jsonl"),
    )
    check(out["exit"] == 0, "撤销 + 重录同一 calibration_id 成功")
    check(hourly(db) == EXPECT_HOURLY_AFTER_CAL3, "重录后小时结果恢复")
    run_oracle(db)

    print("\n== 6) 重传 / 冲突 / 重叠拒绝 ==")
    before_counts = run_cli("status", "--db", str(db))["counts"]
    out = run_cli("ingest", "--db", str(db), "--file", str(INPUT / "readings.jsonl"))
    check(out["exit"] == 0 and out["inserted"] == 0, "整份原样重传不增加样本")
    check(sorted(out["duplicates"]) == sorted(EXPECT_REASONS_AFTER_INGEST),
          "原样重传的 11 个 reading_id 全部识别为幂等重复")
    out = run_cli(
        "ingest", "--db", str(db),
        "--file", str(INPUT / "retransmit_same_instant.jsonl"),
    )
    check(out["exit"] == 0 and out["inserted"] == 0,
          "同一瞬间用 +08:00 表示重传也被识别为同内容（规范化比较）")
    after_counts = run_cli("status", "--db", str(db))["counts"]
    check(after_counts["sensor_readings"] == before_counts["sensor_readings"],
          "重传后样本数不变")
    out = run_cli("ingest", "--db", str(db), "--file", str(INPUT / "conflict_rejected.jsonl"))
    check(out["exit"] == 2 and out["status"] == "error", "同号不同内容被拒绝")
    check(out["errors"][0]["code"] == "reading_conflict", "错误码 reading_conflict")
    check(out["inserted"] == 0 and run_cli("status", "--db", str(db))["counts"]
          ["sensor_readings"] == after_counts["sensor_readings"],
          "同一批中的新读数 s1-r99 也随冲突整批撤销")
    out = run_cli("calibrate", "--db", str(db), "--file", str(INPUT / "overlap_rejected.jsonl"))
    check(out["exit"] == 2 and out["errors"][0]["code"] == "calibration_overlap",
          "同设备相交时段被拒绝")
    check(out.get("rolled_back") is True, "拒绝时整批回滚")
    check(hourly(db) == EXPECT_HOURLY_AFTER_CAL3, "拒绝/回滚未改变小时结果")
    run_oracle(db)

    print("\n== 7) 处理中途真实崩溃：三层一起恢复 ==")
    cal_file = tmp / "crash_cal.jsonl"
    cal_file.write_text(json.dumps({
        "calibration_id": "s1-early", "sensor_id": "s1",
        "valid_from": "2026-03-01T09:00:00Z", "valid_to": "2026-03-01T10:00:00Z",
        "gain": 9.0, "offset": 9.0,
    }) + "\n", encoding="utf-8")
    before_crash = three_layers(db)
    proc = subprocess.run(
        [sys.executable, "-I", str(CRASH_CHILD), str(db), str(cal_file)],
        capture_output=True, text=True, cwd=ROOT,
    )
    check(proc.returncode == -signal.SIGKILL or proc.returncode == 137,
          f"子进程在事务中途被 SIGKILL（returncode={proc.returncode}）")
    after_crash = three_layers(db)
    check(after_crash == before_crash,
          "重开库：校准/明细结论/小时指标三层完全停留在操作前（无部分提交）")
    check(after_crash["cal"][0][3] == 1.1, "校准系数仍是旧值 1.1")
    check(run_oracle(db)["status"] == "ok", "崩溃恢复后独立对照仍一致")
    # 恢复正常执行同一操作，证明库未被锁死/半坏
    out = run_cli("calibrate", "--db", str(db), "--file", str(cal_file))
    check(out["exit"] == 0, "崩溃后重新执行同一操作成功")
    check(out["affected"]["readings_recomputed"] >= 1, "重新执行确实重算了读数")
    check(hourly(db) != EXPECT_HOURLY_AFTER_CAL3, "新系数确实生效（报表随明细更新）")
    check(run_oracle(db)["status"] == "ok", "重执行后独立对照一致")
    # 恢复原值，回到演示的稳定状态
    (tmp / "restore.jsonl").write_text(
        json.dumps({
            "calibration_id": "s1-early", "sensor_id": "s1",
            "valid_from": "2026-03-01T09:00:00Z", "valid_to": "2026-03-01T10:00:00Z",
            "gain": 1.1, "offset": 0.5,
        }) + "\n",
        encoding="utf-8",
    )
    run_cli("calibrate", "--db", str(db), "--file", str(tmp / "restore.jsonl"))
    check(hourly(db) == EXPECT_HOURLY_AFTER_CAL3, "恢复原系数后报表回到演示状态")
    run_oracle(db)

    print(f"\n演示完成：库文件 {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
