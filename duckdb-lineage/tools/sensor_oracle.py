"""独立全量对照实现（oracle）。

刻意不复用 ``sensorpipe`` 的 SQL 区间匹配、结论回填与小时重算代码：
从原始读数与校准记录出发，用纯 Python 重新做一次全量计算，作为回归对照。

匹配规则（与产品需求一致，独立实现）：

  * 只考虑 revoked = FALSE 的校准；
  * 同一设备内按 [valid_from, valid_to) 半开区间匹配；
  * 修正温度 = 原始温度 * gain + offset；
  * status <> 'OK' -> fault；没有命中校准 -> missing_calibration；
    修正温度不在 [-50, 80] -> out_of_range；否则 included；
  * 小时按 UTC 的整点划分并聚合纳入样本。

用法：
    python3 -I tools/sensor_oracle.py --db /path/sensor.duckdb
    python3 -I tools/sensor_oracle.py --db /path/sensor.duckdb --check

``--check`` 只输出 {"status":"ok","mismatches":[]}，退出码 0；若与库内
``sensor_reading_results`` / ``sensor_hourly`` 有任何不一致，逐条列出并退出 1。
"""

from __future__ import annotations

import argparse
import json
import math
import site
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    for _p in (site.getusersitepackages(), *site.getsitepackages()):
        if _p and _p not in sys.path:
            sys.path.append(_p)

import duckdb  # noqa: E402

UTC = timezone.utc
REASON_INCLUDED = "included"
REASON_FAULT = "fault"
REASON_MISSING = "missing_calibration"
REASON_RANGE = "out_of_range"


def _utc(dt: datetime) -> datetime:
    # 库内统一以 UTC 墙上时间的 TIMESTAMP 保存；naive 即按 UTC 解释
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _hour(dt: datetime) -> datetime:
    return _utc(dt).replace(minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_expected(con: duckdb.DuckDBPyConnection) -> dict:
    """从原始表全量计算期望的明细结论与小时指标。"""
    readings = con.execute(
        "SELECT reading_id, sensor_id, measured_at, temperature, battery, status "
        "FROM main.sensor_readings ORDER BY reading_id"
    ).fetchall()
    calibrations = con.execute(
        "SELECT calibration_id, sensor_id, valid_from, valid_to, gain, offset_value "
        "FROM main.sensor_calibrations WHERE NOT revoked "
        "ORDER BY sensor_id, valid_from, calibration_id"
    ).fetchall()

    cals_by_sensor: dict[str, list[tuple]] = {}
    for cid, sensor_id, start, end, gain, offset in calibrations:
        if not (math.isfinite(gain) and math.isfinite(offset)) or end <= start:
            raise AssertionError(f"库内校准记录不合法: {cid}")
        cals_by_sensor.setdefault(sensor_id, []).append(
            (cid, _utc(start), _utc(end), float(gain), float(offset))
        )

    results: dict[str, dict] = {}
    buckets: dict[tuple[str, datetime], list[tuple[float, float]]] = {}
    for reading_id, sensor_id, measured_at, temperature, battery, status in readings:
        measured = _utc(measured_at)
        matched = None
        # 独立线性扫描：不调用线上任何区间查询/回填函数
        for cid, start, end, gain, offset in cals_by_sensor.get(sensor_id, []):
            if start <= measured < end:
                matched = (cid, gain, offset)
                break
        corrected = None
        # 只要命中校准就给出修正值（故障读数也保留，便于追溯；只是不进指标）
        if matched is not None:
            corrected = round(float(temperature) * matched[1] + matched[2], 12)
        if status != "OK":
            reason = REASON_FAULT
        elif matched is None:
            reason = REASON_MISSING
        else:
            reason = (
                REASON_INCLUDED
                if -50.0 <= corrected <= 80.0
                else REASON_RANGE
            )
        included = reason == REASON_INCLUDED
        hour = _hour(measured)
        results[reading_id] = {
            "reading_id": reading_id,
            "sensor_id": sensor_id,
            "measured_at": _iso(measured),
            "hour_utc": _iso(hour),
            "temperature_original": float(temperature),
            "battery": float(battery),
            "status": status,
            "calibration_id": matched[0] if matched else None,
            "temperature_corrected": corrected,
            "included": included,
            "reason": reason,
        }
        if included:
            buckets.setdefault((sensor_id, hour), []).append((corrected, float(battery)))

    hourly = []
    for (sensor_id, hour), values in sorted(buckets.items()):
        temps = [v[0] for v in values]
        hourly.append(
            {
                "sensor_id": sensor_id,
                "hour_utc": _iso(hour),
                "samples": len(values),
                "avg_temperature": round(sum(temps) / len(temps), 12),
                "min_battery": min(v[1] for v in values),
            }
        )
    return {"readings": results, "hourly": hourly}


def _db_view(con: duckdb.DuckDBPyConnection) -> dict:
    readings = {
        row[0]: {
            "reading_id": row[0],
            "sensor_id": row[1],
            "measured_at": _iso(row[2]),
            "hour_utc": _iso(row[3].replace(tzinfo=UTC)),
            "temperature_original": float(row[4]),
            "battery": float(row[5]),
            "status": row[6],
            "calibration_id": row[7],
            "temperature_corrected": float(row[8]) if row[8] is not None else None,
            "included": bool(row[9]),
            "reason": row[10],
        }
        for row in con.execute(
            "SELECT reading_id, sensor_id, measured_at, hour_utc, "
            "temperature_original, battery, status, calibration_id, "
            "temperature_corrected, included, reason "
            "FROM main.sensor_reading_results"
        ).fetchall()
    }
    hourly = [
        {
            "sensor_id": row[0],
            "hour_utc": _iso(row[1].replace(tzinfo=UTC)),
            "samples": int(row[2]),
            "avg_temperature": float(row[3]),
            "min_battery": float(row[4]),
        }
        for row in con.execute(
            "SELECT sensor_id, hour_utc, samples, avg_temperature, min_battery "
            "FROM main.sensor_hourly ORDER BY sensor_id, hour_utc"
        ).fetchall()
    ]
    return {"readings": readings, "hourly": hourly}


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-12 * max(1.0, abs(a), abs(b))


def compare(db_path: str | Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        expected = compute_expected(con)
        actual = _db_view(con)
    finally:
        con.close()
    mismatches: list[dict] = []
    exp_r, act_r = expected["readings"], actual["readings"]
    for reading_id in sorted(set(exp_r) | set(act_r)):
        if reading_id not in act_r:
            mismatches.append({"kind": "missing_result", "reading_id": reading_id})
            continue
        if reading_id not in exp_r:
            mismatches.append({"kind": "extra_result", "reading_id": reading_id})
            continue
        e, a = exp_r[reading_id], act_r[reading_id]
        diffs = {}
        for key in ("sensor_id", "measured_at", "hour_utc", "battery", "status",
                    "calibration_id", "included", "reason"):
            if e[key] != a[key]:
                diffs[key] = {"expected": e[key], "actual": a[key]}
        if not _close(e["temperature_original"], a["temperature_original"]):
            diffs["temperature_original"] = {
                "expected": e["temperature_original"], "actual": a["temperature_original"]
            }
        if not _close(e["temperature_corrected"], a["temperature_corrected"]):
            diffs["temperature_corrected"] = {
                "expected": e["temperature_corrected"],
                "actual": a["temperature_corrected"],
            }
        if diffs:
            mismatches.append({"kind": "reading", "reading_id": reading_id, "diffs": diffs})

    exp_h = {(h["sensor_id"], h["hour_utc"]): h for h in expected["hourly"]}
    act_h = {(h["sensor_id"], h["hour_utc"]): h for h in actual["hourly"]}
    for key in sorted(set(exp_h) | set(act_h)):
        if key not in act_h:
            mismatches.append({"kind": "missing_hour", "sensor_id": key[0], "hour_utc": key[1]})
        elif key not in exp_h:
            mismatches.append({"kind": "extra_hour", "sensor_id": key[0], "hour_utc": key[1]})
        else:
            e, a = exp_h[key], act_h[key]
            diffs = {}
            if e["samples"] != a["samples"]:
                diffs["samples"] = {"expected": e["samples"], "actual": a["samples"]}
            if not _close(e["avg_temperature"], a["avg_temperature"]):
                diffs["avg_temperature"] = {
                    "expected": e["avg_temperature"], "actual": a["avg_temperature"]
                }
            if not _close(e["min_battery"], a["min_battery"]):
                diffs["min_battery"] = {
                    "expected": e["min_battery"], "actual": a["min_battery"]
                }
            if diffs:
                mismatches.append(
                    {"kind": "hour", "sensor_id": key[0], "hour_utc": key[1], "diffs": diffs}
                )
    return {"status": "ok" if not mismatches else "mismatch", "mismatches": mismatches}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立全量对照计算")
    parser.add_argument("--db", required=True)
    parser.add_argument("--check", action="store_true", help="与库内结果逐项比对")
    parser.add_argument("--output", choices=["json"], default="json")
    args = parser.parse_args(argv)
    if args.check:
        payload = compare(args.db)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
        return 0 if payload["status"] == "ok" else 1
    con = duckdb.connect(args.db, read_only=True)
    try:
        payload = {"status": "ok", **compute_expected(con)}
    finally:
        con.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
