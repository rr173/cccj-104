"""核心事务流程。

三条入口：

  import_readings     读数 JSONL；同号同内容 -> 幂等跳过；同号不同内容 ->
                      整批撤销并报冲突。原有读数只做结论补算（新校准/新读数）。
  apply_calibrations  校准 JSONL（新增/修改/恢复 + 撤销行）；整批成功后重算
                      受影响读数与小时指标；失败整批回滚。
  rebuild_all         仅用于恢复核对/离线重算（不是业务入口）：从原始读数与
                      校准全量重算，方便与独立对照实现比对。

所有写操作（读数、校准、读数结论、小时指标、影响范围登记）都在同一个
DuckDB 事务里提交；任何一步失败一律 ROLLBACK。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import duckdb

from .db import connect, require_initialized
from .errors import DomainError, err
from .inputs import (
    Calibration,
    CalibrationOp,
    Reading,
    Revocation,
    parse_calibration_line,
    parse_lines,
    parse_reading_line,
)
from .util import fmt_time, to_naive_utc

UTC = timezone.utc

REASON_INCLUDED = "included"
REASON_FAULT = "fault"
REASON_MISSING = "missing_calibration"
REASON_RANGE = "out_of_range"
TEMP_MIN = -50.0
TEMP_MAX = 80.0


# ---------------------------------------------------------------------------
# 结论计算（SQL 中的权威表达式；oracle 另写 Python 版，不复用这里）


def _conclusion_sql(reading_alias: str = "r", cal_alias: str = "c") -> str:
    """给定一行读数与可能命中的校准，产生结论列。

    约定：``c.calibration_id`` 为 NULL 表示未命中校准（LEFT JOIN 未匹配）。
    """
    return f"""
        CASE
          WHEN {reading_alias}.status <> 'OK' THEN '{REASON_FAULT}'
          WHEN {cal_alias}.calibration_id IS NULL THEN '{REASON_MISSING}'
          WHEN ({reading_alias}.temperature * {cal_alias}.gain
                + {cal_alias}.offset_value) NOT BETWEEN {TEMP_MIN} AND {TEMP_MAX}
            THEN '{REASON_RANGE}'
          ELSE '{REASON_INCLUDED}'
        END
    """


def _corrected_sql(reading_alias: str = "r", cal_alias: str = "c") -> str:
    """修正温度乘加后做一次 12 位小数舍入，消除浮点尾差（如 24.700000000000003）。

    规格本身是双精度乘加；舍入只抹掉二进制尾差，不改变业务精度，也让
    均值与独立对照（同样按 12 位比较）稳定可比。
    """
    return (
        f"CASE WHEN {cal_alias}.calibration_id IS NULL THEN NULL "
        f"ELSE round({reading_alias}.temperature * {cal_alias}.gain "
        f"+ {cal_alias}.offset_value, 12) END"
    )


# 命中校准：同设备、未撤销、measured_at ∈ [valid_from, valid_to)。
# 正常情况下区间不相交，每条读数至多命中一条；LATERAL + ORDER BY + LIMIT 1
# 保证即使库被人绕过校验写进相交区间，结论也只取最早生效的一条（确定性）。
CAL_LATERAL = (
    "SELECT c2.calibration_id, c2.gain, c2.offset_value "
    "FROM main.sensor_calibrations c2 "
    "WHERE c2.sensor_id = r.sensor_id AND NOT c2.revoked "
    "AND r.measured_at >= c2.valid_from AND r.measured_at < c2.valid_to "
    "ORDER BY c2.valid_from, c2.calibration_id LIMIT 1"
)


# ---------------------------------------------------------------------------
# 小时指标重算：对给定 (sensor_id, hour_utc) 集合，先删后插，空组自然消失


@dataclass
class HourChange:
    sensor_id: str
    hour_utc: datetime
    change: str
    samples_before: int | None
    samples_after: int | None
    avg_before: float | None
    avg_after: float | None
    min_battery_before: float | None
    min_battery_after: float | None


def _hour_keys_table(
    con: duckdb.DuckDBPyConnection, key_list: list[tuple[str, datetime]]
) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _sp_hour_keys (sensor_id VARCHAR, hour_utc TIMESTAMP)"
    )
    con.executemany("INSERT INTO _sp_hour_keys VALUES (?, ?)", key_list)


def _hour_snapshot(
    con: duckdb.DuckDBPyConnection,
    keys: Iterable[tuple[str, datetime]],
) -> dict[tuple[str, datetime], tuple]:
    key_list = sorted(set(keys))
    if not key_list:
        return {}
    _hour_keys_table(con, key_list)
    rows = con.execute(
        """
        SELECT h.sensor_id, h.hour_utc, h.samples, h.avg_temperature,
               h.min_battery, h.revision
        FROM main.sensor_hourly h
        JOIN _sp_hour_keys k
          ON k.sensor_id = h.sensor_id AND k.hour_utc = h.hour_utc
        """
    ).fetchall()
    return {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in rows}


def recompute_hours(
    con: duckdb.DuckDBPyConnection,
    keys: Iterable[tuple[str, datetime]],
) -> dict[tuple[str, datetime], HourChange]:
    """重算指定小时：从读数结论即时聚合，把旧行改成新值或删除空组。

    - 空组不留下旧行（即"删掉最后一个样本所在的小时"）；
    - 内容未变的小时既不重写、也不提升 revision；因此即使某设备的区间
      被触碰，没用到的其它小时/其它设备不可能被改写。
    """
    key_list = sorted(set(keys))
    if not key_list:
        return {}
    before = _hour_snapshot(con, key_list)
    desired = {
        (r[0], r[1]): (r[2], r[3], r[4])
        for r in con.execute(
            """
            SELECT r.sensor_id, r.hour_utc, COUNT(*) AS samples,
                   round(AVG(r.temperature_corrected), 12) AS avg_temperature,
                   MIN(r.battery) AS min_battery
            FROM main.sensor_reading_results r
            JOIN _sp_hour_keys k
              ON k.sensor_id = r.sensor_id AND k.hour_utc = r.hour_utc
            WHERE r.included
            GROUP BY r.sensor_id, r.hour_utc
            """
        ).fetchall()
    }
    changes: dict[tuple[str, datetime], HourChange] = {}
    for key in key_list:
        old = before.get(key)
        new_vals = desired.get(key)
        old_vals = old[:3] if old else None
        if old_vals is None and new_vals is None:
            change = "unchanged"
        elif old_vals is None:
            change = "created"
        elif new_vals is None:
            change = "deleted"
        elif old_vals == new_vals:
            change = "unchanged"
        else:
            change = "updated"

        if change == "created":
            con.execute(
                "INSERT INTO main.sensor_hourly "
                "(sensor_id, hour_utc, samples, avg_temperature, min_battery, revision) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                [key[0], key[1], *new_vals],
            )
        elif change == "updated":
            con.execute(
                "UPDATE main.sensor_hourly SET samples = ?, avg_temperature = ?, "
                "min_battery = ?, revision = revision + 1 "
                "WHERE sensor_id = ? AND hour_utc = ?",
                [*new_vals, key[0], key[1]],
            )
        elif change == "deleted":
            con.execute(
                "DELETE FROM main.sensor_hourly "
                "WHERE sensor_id = ? AND hour_utc = ?",
                [key[0], key[1]],
            )
        # unchanged：不写库

        changes[key] = HourChange(
            sensor_id=key[0],
            hour_utc=key[1],
            change=change,
            samples_before=old_vals[0] if old_vals else None,
            samples_after=new_vals[0] if new_vals else None,
            avg_before=old_vals[1] if old_vals else None,
            avg_after=new_vals[1] if new_vals else None,
            min_battery_before=old_vals[2] if old_vals else None,
            min_battery_after=new_vals[2] if new_vals else None,
        )
    return changes


# ---------------------------------------------------------------------------
# 读数结论重算：对一组 reading_id（或设备集合）重新 join 校准并写回结论


def _result_rows(
    con: duckdb.DuckDBPyConnection,
    reading_ids: list[str] | None,
) -> list[tuple]:
    """重新计算待写回的结果行（不写库）。"""
    where = ""
    if reading_ids is not None:
        if not reading_ids:
            return []
        con.execute("CREATE OR REPLACE TEMP TABLE _sp_result_ids (reading_id VARCHAR)")
        con.executemany(
            "INSERT INTO _sp_result_ids VALUES (?)", [[x] for x in sorted(set(reading_ids))]
        )
        where = "WHERE r.reading_id IN (SELECT reading_id FROM _sp_result_ids)"
    return con.execute(
        f"""
        SELECT r.reading_id, r.sensor_id, r.measured_at,
               date_trunc('hour', r.measured_at) AS hour_utc,
               r.temperature, r.battery, r.status,
               c.calibration_id,
               {_corrected_sql()} AS corrected,
               {_conclusion_sql()} AS reason
        FROM main.sensor_readings r
        LEFT JOIN LATERAL ({CAL_LATERAL}) c ON TRUE
        {where}
        ORDER BY r.reading_id
        """
    ).fetchall()


def _result_snapshot(
    con: duckdb.DuckDBPyConnection, reading_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not reading_ids:
        return {}
    con.execute("CREATE OR REPLACE TEMP TABLE _sp_snapshot_ids (reading_id VARCHAR)")
    con.executemany("INSERT INTO _sp_snapshot_ids VALUES (?)", [[x] for x in reading_ids])
    return {
        r[0]: {
            "calibration_id": r[1],
            "temperature_corrected": r[2],
            "included": r[3],
            "reason": r[4],
        }
        for r in con.execute(
            "SELECT t.reading_id, t.calibration_id, t.temperature_corrected, "
            "t.included, t.reason "
            "FROM main.sensor_reading_results t "
            "JOIN _sp_snapshot_ids s ON s.reading_id = t.reading_id"
        ).fetchall()
    }


def _write_results(
    con: duckdb.DuckDBPyConnection,
    rows: list[tuple],
) -> dict[str, dict[str, Any]]:
    """把结论行 upsert 进 sensor_reading_results，返回每条的 before/after。"""
    row_ids = [r[0] for r in rows]
    current = _result_snapshot(con, row_ids)
    con.execute("CREATE OR REPLACE TEMP TABLE _sp_new_results ("
                "reading_id VARCHAR, sensor_id VARCHAR, measured_at TIMESTAMP, "
                "hour_utc TIMESTAMP, temperature_original DOUBLE, battery DOUBLE, "
                "status VARCHAR, calibration_id VARCHAR, temperature_corrected DOUBLE, "
                "included BOOLEAN, reason VARCHAR)")
    if rows:
        con.executemany(
            "INSERT INTO _sp_new_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                    r[9] == REASON_INCLUDED, r[9],
                )
                for r in rows
            ],
        )
    # 只更新内容真的变了的行（revision+1），未变化保持 revision
    con.execute(
        """
        UPDATE main.sensor_reading_results t
        SET calibration_id = n.calibration_id,
            temperature_corrected = n.temperature_corrected,
            included = n.included,
            reason = n.reason,
            revision = t.revision + 1
        FROM _sp_new_results n
        WHERE t.reading_id = n.reading_id
          AND (t.calibration_id IS DISTINCT FROM n.calibration_id
               OR t.temperature_corrected IS DISTINCT FROM n.temperature_corrected
               OR t.included IS DISTINCT FROM n.included
               OR t.reason IS DISTINCT FROM n.reason)
        """
    )
    # 只对缺行的 reading_id 插入（sensor/measured/hour/原值/电量/状态）
    con.execute(
        """
        INSERT INTO main.sensor_reading_results
            (reading_id, sensor_id, measured_at, hour_utc, temperature_original,
             battery, status, calibration_id, temperature_corrected, included,
             reason, revision)
        SELECT n.reading_id, n.sensor_id, n.measured_at, n.hour_utc,
               n.temperature_original, n.battery, n.status, n.calibration_id,
               n.temperature_corrected, n.included, n.reason, 1
        FROM _sp_new_results n
        LEFT JOIN main.sensor_reading_results t ON t.reading_id = n.reading_id
        WHERE t.reading_id IS NULL
        """
    )
    after = _result_snapshot(con, row_ids)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        rid = r[0]
        before = current.get(rid)
        now = after[rid]
        out[rid] = {
            "reading_id": rid,
            "sensor_id": r[1],
            "measured_at": r[2],
            "hour_utc": r[3],
            "calibration_before": before["calibration_id"] if before else None,
            "calibration_after": now["calibration_id"],
            "corrected_before": before["temperature_corrected"] if before else None,
            "corrected_after": now["temperature_corrected"],
            "reason_before": before["reason"] if before else None,
            "reason_after": now["reason"],
            "included_before": before["included"] if before else None,
            "included_after": now["included"],
            "changed": before is None
            or before["calibration_id"] != now["calibration_id"]
            or before["temperature_corrected"] != now["temperature_corrected"]
            or before["included"] != now["included"]
            or before["reason"] != now["reason"],
        }
    return out


# ---------------------------------------------------------------------------
# 影响范围登记


def _new_run_id(con: duckdb.DuckDBPyConnection, prefix: str) -> str:
    row = con.execute("SELECT COUNT(*) FROM main.sensor_processing_runs").fetchone()
    return f"{prefix}-{int(row[0]) + 1:06d}"


def _record_run(
    con: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    kind: str,
    payload_sha256: str,
    reading_changes: dict[str, dict[str, Any]],
    hour_changes: dict[tuple[str, datetime], HourChange],
    extra: dict[str, Any],
) -> dict[str, Any]:
    rows_r = list(reading_changes.values())
    rows_h = list(hour_changes.values())
    readings_changed = sum(1 for r in rows_r if r["changed"])
    hours_changed = sum(1 for h in rows_h if h.change in ("created", "updated", "deleted"))
    hours_deleted = sum(1 for h in rows_h if h.change == "deleted")
    if rows_r:
        con.executemany(
            "INSERT INTO main.sensor_run_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, r["reading_id"], r["calibration_before"],
                    r["calibration_after"], r["corrected_before"],
                    r["corrected_after"], r["reason_before"], r["reason_after"],
                    r["included_before"], r["included_after"], r["changed"],
                )
                for r in rows_r
            ],
        )
    if rows_h:
        con.executemany(
            "INSERT INTO main.sensor_run_hours VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, h.sensor_id, h.hour_utc, h.change, h.samples_before,
                    h.samples_after, h.avg_before, h.avg_after,
                    h.min_battery_before, h.min_battery_after,
                )
                for h in rows_h
            ],
        )
    details = {
        "readings_recomputed": len(rows_r),
        "readings_changed": readings_changed,
        "readings": [
            {
                "reading_id": r["reading_id"],
                "sensor_id": r["sensor_id"],
                "measured_at": fmt_time(r["measured_at"]),
                "hour_utc": fmt_time(r["hour_utc"].replace(tzinfo=UTC)),
                "calibration_before": r["calibration_before"],
                "calibration_after": r["calibration_after"],
                "temperature_corrected_before": r["corrected_before"],
                "temperature_corrected_after": r["corrected_after"],
                "reason_before": r["reason_before"],
                "reason_after": r["reason_after"],
                "included_before": r["included_before"],
                "included_after": r["included_after"],
                "changed": r["changed"],
            }
            for r in rows_r
        ],
        "hours_recomputed": len(rows_h),
        "hours_changed": hours_changed,
        "hours_deleted": hours_deleted,
        "hours": [
            {
                "sensor_id": h.sensor_id,
                "hour_utc": fmt_time(h.hour_utc.replace(tzinfo=UTC)),
                "change": h.change,
                "samples_before": h.samples_before,
                "samples_after": h.samples_after,
                "avg_temperature_before": h.avg_before,
                "avg_temperature_after": h.avg_after,
                "min_battery_before": h.min_battery_before,
                "min_battery_after": h.min_battery_after,
            }
            for h in rows_h
        ],
        **extra,
    }
    con.execute(
        "INSERT INTO main.sensor_processing_runs "
        "(run_id, kind, submitted_at, payload_sha256, status, "
        " readings_recomputed, readings_changed, hours_recomputed, hours_changed, "
        " hours_deleted, details_json) "
        "VALUES (?, ?, ?, ?, 'committed', ?, ?, ?, ?, ?, ?)",
        [
            run_id,
            kind,
            to_naive_utc(datetime.now(UTC)),
            payload_sha256,
            len(rows_r),
            readings_changed,
            len(rows_h),
            hours_changed,
            hours_deleted,
            json.dumps(details, ensure_ascii=False, sort_keys=True),
        ],
    )
    return {
        "run_id": run_id,
        "kind": kind,
        "payload_sha256": payload_sha256,
        "readings_recomputed": len(rows_r),
        "readings_changed": readings_changed,
        "hours_recomputed": len(rows_h),
        "hours_changed": hours_changed,
        "hours_deleted": hours_deleted,
        "affected_readings": [
            r["reading_id"] for r in rows_r
        ],
        "affected_hours": [
            {"sensor_id": h.sensor_id, "hour_utc": fmt_time(h.hour_utc.replace(tzinfo=UTC)),
             "change": h.change}
            for h in rows_h
        ],
        **extra,
    }


# ---------------------------------------------------------------------------
# 冲突检测：同 reading_id 不同内容


def _db_reading(con: duckdb.DuckDBPyConnection, reading_id: str) -> Reading | None:
    row = con.execute(
        "SELECT sensor_id, measured_at, temperature, battery, status "
        "FROM main.sensor_readings WHERE reading_id = ?",
        [reading_id],
    ).fetchone()
    if row is None:
        return None
    return Reading(
        reading_id=reading_id,
        sensor_id=row[0],
        measured_at=row[1].replace(tzinfo=UTC),
        temperature=row[2],
        battery=row[3],
        status=row[4],
    )


def _batch_conflicts(
    con: duckdb.DuckDBPyConnection, readings: list[Reading]
) -> tuple[list[Reading], list[dict[str, Any]], list[str]]:
    """返回 (需插入, 冲突错误, 幂等命中)。冲突包括库内不同内容与批内同号不同内容。"""
    to_insert: list[Reading] = []
    errors: list[dict[str, Any]] = []
    duplicates: list[str] = []
    seen: dict[str, Reading] = {}
    for reading in readings:
        prior = seen.get(reading.reading_id)
        if prior is not None:
            if prior.content_hash == reading.content_hash:
                duplicates.append(reading.reading_id)
                continue
            errors.append(
                err(
                    "reading_conflict",
                    "同一批次内 reading_id 重复但内容不同；本次导入已撤销",
                    reading_id=reading.reading_id,
                    existing=prior.canonical_json(),
                    incoming=reading.canonical_json(),
                )
            )
            continue
        existing = _db_reading(con, reading.reading_id)
        if existing is not None:
            if existing.content_hash == reading.content_hash:
                duplicates.append(reading.reading_id)
                seen[reading.reading_id] = reading
                continue
            errors.append(
                err(
                    "reading_conflict",
                    "库内已有同 reading_id 但内容不同；本次导入已撤销，原读数不变",
                    reading_id=reading.reading_id,
                    existing=existing.canonical_json(),
                    incoming=reading.canonical_json(),
                )
            )
            continue
        seen[reading.reading_id] = reading
        to_insert.append(reading)
    return to_insert, errors, duplicates


# ---------------------------------------------------------------------------
# 校准输入校验与写入


def _validate_calibration_batch(
    con: duckdb.DuckDBPyConnection, ops: list[CalibrationOp]
) -> tuple[list[Calibration], list[Revocation], list[dict[str, Any]]]:
    """把本批操作限制成"最终状态"，再检出生效区间相交。

    同批内对同一 calibration_id 多次写（例如先撤销再恢复）以后出现者为准，
    这正是"撤销后重录恢复"要用到的语义；不同 calibration_id 互相重叠则拒绝。
    """
    errors: list[dict[str, Any]] = []
    final: dict[str, Calibration | None] = {}
    order: list[str] = []
    for op in ops:
        if isinstance(op, Revocation):
            cid = op.calibration_id
            if cid not in final:
                order.append(cid)
            final[cid] = None
        else:
            cid = op.calibration_id
            if cid not in final:
                order.append(cid)
            final[cid] = op

    # 现有未撤销记录（排除本批触及的 id）作为背景约束
    background: dict[str, tuple[str, datetime, datetime, str]] = {}
    for row in con.execute(
        "SELECT calibration_id, sensor_id, valid_from, valid_to, calibration_id "
        "FROM main.sensor_calibrations WHERE NOT revoked"
    ).fetchall():
        if row[0] in final:
            continue
        background[row[0]] = (
            row[1],
            row[2].replace(tzinfo=UTC),
            row[3].replace(tzinfo=UTC),
            row[0],
        )

    active = dict(background)
    writes: list[Calibration] = []
    revokes: list[Revocation] = []
    for cid in order:
        op = final[cid]
        if op is None:
            revokes.append(Revocation(cid))
            continue
        # 传感器身份不可被改到另一台设备后悄悄移动历史区间：
        # 修改只允许原 calibration_id 保持同一 sensor_id（见文档）。
        existing = con.execute(
            "SELECT sensor_id FROM main.sensor_calibrations WHERE calibration_id = ?",
            [cid],
        ).fetchone()
        if existing is not None and existing[0] != op.sensor_id:
            errors.append(
                err(
                    "calibration_sensor_mismatch",
                    f"calibration_id {cid!r} 已属于设备 {existing[0]!r}，"
                    f"不能改成 {op.sensor_id!r}；请撤销旧记录并新建",
                    calibration_id=cid,
                    existing_sensor_id=existing[0],
                    incoming_sensor_id=op.sensor_id,
                )
            )
            continue
        writes.append(op)
        active[cid] = (op.sensor_id, op.valid_from, op.valid_to, cid)

    # 生效区间相交检查（半开区间 [from, to)；端点相接允许）
    grouped: dict[str, list[tuple[datetime, datetime, str]]] = {}
    for cid, (sensor_id, vf, vt, _cid) in active.items():
        grouped.setdefault(sensor_id, []).append((vf, vt, cid))
    for sensor_id, intervals in sorted(grouped.items()):
        intervals.sort()
        for i in range(len(intervals) - 1):
            a_from, a_to, a_id = intervals[i]
            b_from, b_to, b_id = intervals[i + 1]
            if b_from >= a_to:
                continue
            errors.append(
                    err(
                        "calibration_overlap",
                        f"设备 {sensor_id!r} 的校准时段的交集非空: "
                        f"{a_id} [{fmt_time(a_from)}, {fmt_time(a_to)}) 与 "
                        f"{b_id} [{fmt_time(b_from)}, {fmt_time(b_to)})",
                        sensor_id=sensor_id,
                        calibration_ids=[a_id, b_id],
                    )
                )
    for cid in final:
        if final[cid] is None:
            existing = con.execute(
                "SELECT 1 FROM main.sensor_calibrations WHERE calibration_id = ?",
                [cid],
            ).fetchone()
            if existing is None:
                errors.append(
                    err(
                        "unknown_calibration",
                        f"不能撤销不存在的 calibration_id {cid!r}",
                        calibration_id=cid,
                    )
                )
    return writes, revokes, errors


def _same_calibration(existing: tuple | None, cal: Calibration) -> bool:
    if existing is None or existing[5]:
        return False
    return (
        existing[0] == cal.sensor_id
        and existing[1] == to_naive_utc(cal.valid_from)
        and existing[2] == to_naive_utc(cal.valid_to)
        and existing[3] == cal.gain
        and existing[4] == cal.offset
    )


def _apply_calibration_writes(
    con: duckdb.DuckDBPyConnection,
    writes: list[Calibration],
    revokes: list[Revocation],
) -> list[str]:
    """写入校准；只有内容真的变化的记录才计入"受影响 id"。

    重复提交同一条未改动的校准不会造成任何重算，也不会出现在影响范围里，
    这正是"无关记录不被改写"的要求。
    """
    touched_ids: list[str] = []
    for cal in writes:
        existing = con.execute(
            "SELECT sensor_id, valid_from, valid_to, gain, offset_value, revoked "
            "FROM main.sensor_calibrations WHERE calibration_id = ?",
            [cal.calibration_id],
        ).fetchone()
        if _same_calibration(existing, cal):
            continue
        con.execute(
            """
            INSERT INTO main.sensor_calibrations
                (calibration_id, sensor_id, valid_from, valid_to, gain,
                 offset_value, revoked, revision)
            VALUES (?, ?, ?, ?, ?, ?, FALSE, 1)
            ON CONFLICT (calibration_id) DO UPDATE SET
                sensor_id = excluded.sensor_id,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                gain = excluded.gain,
                offset_value = excluded.offset_value,
                revoked = FALSE,
                revision = main.sensor_calibrations.revision + 1
            """,
            [
                cal.calibration_id,
                cal.sensor_id,
                to_naive_utc(cal.valid_from),
                to_naive_utc(cal.valid_to),
                cal.gain,
                cal.offset,
            ],
        )
        touched_ids.append(cal.calibration_id)
    for rev in revokes:
        was_active = con.execute(
            "SELECT 1 FROM main.sensor_calibrations "
            "WHERE calibration_id = ? AND NOT revoked",
            [rev.calibration_id],
        ).fetchone()
        if was_active is None:
            continue  # 已撤销：幂等，不计入影响范围
        con.execute(
            "UPDATE main.sensor_calibrations "
            "SET revoked = TRUE, revision = revision + 1 "
            "WHERE calibration_id = ? AND NOT revoked",
            [rev.calibration_id],
        )
        touched_ids.append(rev.calibration_id)
    return sorted(set(touched_ids))


Interval = tuple[str, datetime, datetime]
CalInterval = tuple[str, str, datetime, datetime]  # (calibration_id, sensor_id, from, to)


def _calibration_intervals(
    con: duckdb.DuckDBPyConnection, calibration_ids: list[str] | None = None
) -> list[CalInterval]:
    """读取落库的生效区间（含已撤销记录的历史区间）。

    不传 id 时读取全部记录；传 id 时只取这些 id。
    """
    if calibration_ids is not None and not calibration_ids:
        return []
    where = ""
    if calibration_ids is not None:
        con.execute("CREATE OR REPLACE TEMP TABLE _sp_cal_ids (calibration_id VARCHAR)")
        con.executemany(
            "INSERT INTO _sp_cal_ids VALUES (?)", [[x] for x in calibration_ids]
        )
        where = "WHERE c.calibration_id IN (SELECT calibration_id FROM _sp_cal_ids)"
    rows = con.execute(
        "SELECT c.calibration_id, c.sensor_id, c.valid_from, c.valid_to "
        "FROM main.sensor_calibrations c "
        f"{where}"
    ).fetchall()
    return sorted(set(
        (r[0], r[1], r[2].replace(tzinfo=UTC), r[3].replace(tzinfo=UTC)) for r in rows
    ), key=lambda x: (x[1], x[2], x[3], x[0]))


def _merge_intervals(intervals: list[CalInterval]) -> list[Interval]:
    """合并同一设备上重叠或首尾相接的区间（相邻 [10,11)+[11,12) -> [10,12)）。"""
    grouped: dict[str, list[tuple[datetime, datetime]]] = {}
    for _cid, sensor_id, start, end in intervals:
        grouped.setdefault(sensor_id, []).append((start, end))
    merged: list[Interval] = []
    for sensor_id, spans in sorted(grouped.items()):
        spans.sort()
        cur_from, cur_to = spans[0]
        for start, end in spans[1:]:
            if start <= cur_to:
                cur_to = max(cur_to, end)
            else:
                merged.append((sensor_id, cur_from, cur_to))
                cur_from, cur_to = start, end
        merged.append((sensor_id, cur_from, cur_to))
    return merged


def _readings_in_windows(
    con: duckdb.DuckDBPyConnection,
    intervals: list[Interval],
) -> list[str]:
    if not intervals:
        return []
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _sp_windows "
        "(sensor_id VARCHAR, win_from TIMESTAMP, win_to TIMESTAMP)"
    )
    con.executemany(
        "INSERT INTO _sp_windows VALUES (?, ?, ?)",
        [[sensor_id, to_naive_utc(start), to_naive_utc(end)]
         for sensor_id, start, end in intervals],
    )
    rows = con.execute(
        "SELECT DISTINCT r.reading_id FROM main.sensor_readings r "
        "JOIN _sp_windows w ON w.sensor_id = r.sensor_id "
        "AND r.measured_at >= w.win_from AND r.measured_at < w.win_to "
        "ORDER BY r.reading_id"
    ).fetchall()
    return [r[0] for r in rows]


def _hours_for_readings(
    con: duckdb.DuckDBPyConnection, reading_ids: list[str]
) -> list[tuple[str, datetime]]:
    if not reading_ids:
        return []
    con.execute("CREATE OR REPLACE TEMP TABLE _sp_reading_ids (reading_id VARCHAR)")
    con.executemany(
        "INSERT INTO _sp_reading_ids VALUES (?)", [[x] for x in reading_ids]
    )
    rows = con.execute(
        "SELECT DISTINCT r.sensor_id, "
        "date_trunc('hour', r.measured_at) AS hour_utc "
        "FROM main.sensor_readings r "
        "JOIN _sp_reading_ids i ON i.reading_id = r.reading_id"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------------------
# 对外入口：导入读数


def import_readings(
    db_path: str,
    text: str,
    *,
    source: str = "<stdin>",
) -> tuple[dict, int]:
    con = connect(db_path)
    try:
        require_initialized(con)
        records, parse_errors, text_sha = parse_lines(
            text, parse_reading_line, source=source
        )
        if parse_errors:
            return {"status": "error", "errors": parse_errors}, 2
        if not records:
            return {
                "status": "error",
                "errors": [err("input_error", "输入没有任何读数")],
            }, 2
        con.execute("BEGIN TRANSACTION")
        try:
            to_insert, conflicts, duplicates = _batch_conflicts(con, records)
            if conflicts:
                con.execute("ROLLBACK")
                return {
                    "status": "error",
                    "errors": conflicts,
                    "rolled_back": True,
                    "inserted": 0,
                    "duplicates": duplicates,
                }, 2
            if to_insert:
                con.executemany(
                    "INSERT INTO main.sensor_readings "
                    "(reading_id, sensor_id, measured_at, temperature, battery, "
                    " status, content_sha256, revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    [
                        (
                            r.reading_id, r.sensor_id, to_naive_utc(r.measured_at),
                            r.temperature, r.battery, r.status, r.content_hash,
                        )
                        for r in to_insert
                    ],
                )
                new_ids = [r.reading_id for r in to_insert]
                rows = _result_rows(con, new_ids)
                reading_changes = _write_results(con, rows)
                hour_keys = _hours_for_readings(con, new_ids)
                hour_changes = recompute_hours(con, hour_keys)
                run_id = _new_run_id(con, "ingest")
                run = _record_run(
                    con,
                    run_id=run_id,
                    kind="import_readings",
                    payload_sha256=text_sha,
                    reading_changes=reading_changes,
                    hour_changes=hour_changes,
                    extra={
                        "inserted": len(to_insert),
                        "duplicates": duplicates,
                    },
                )
            else:
                # 全部是幂等重传：仍登记一次处理观察，但不增加样本
                run_id = _new_run_id(con, "ingest")
                run = _record_run(
                    con,
                    run_id=run_id,
                    kind="import_readings",
                    payload_sha256=text_sha,
                    reading_changes={},
                    hour_changes={},
                    extra={
                        "inserted": 0,
                        "duplicates": duplicates,
                    },
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return (
            {
                "status": "ok",
                "run_id": run_id,
                "inserted": len(to_insert),
                "duplicates": duplicates,
                "total_in_input": len(records),
                "affected": run,
            },
            0,
        )
    except DomainError as ex:
        return {"status": "error", "errors": ex.errors}, ex.exit_code
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 对外入口：维护校准


def apply_calibrations(
    db_path: str,
    text: str,
    *,
    source: str = "<stdin>",
    _fault_hook: Any = None,
) -> tuple[dict, int]:
    """维护校准并重算受影响结论/小时。

    ``_fault_hook`` 仅供回归测试注入"处理中途真实崩溃"（例如 os.kill），
    生产 CLI 不传；调用于事务内、部分层已写但尚未提交的时刻。
    """
    con = connect(db_path)
    try:
        require_initialized(con)
        ops, parse_errors, text_sha = parse_lines(
            text, parse_calibration_line, source=source
        )
        if parse_errors:
            return {"status": "error", "errors": parse_errors}, 2
        if not ops:
            return (
                {
                    "status": "error",
                    "errors": [err("input_error", "输入没有任何校准记录/撤销命令")],
                },
                2,
            )
        con.execute("BEGIN TRANSACTION")
        try:
            writes, revokes, errors = _validate_calibration_batch(con, ops)
            if errors:
                con.execute("ROLLBACK")
                return {"status": "error", "errors": errors, "rolled_back": True}, 2
            # 先记下改动前的区间，再落库并取改动后的区间：并集覆盖
            # "旧区间 ∪ 新区间"，缩短/移动区间时旧位置上的样本也会重算。
            candidate_ids = sorted({c.calibration_id for c in writes}
                                   | {r.calibration_id for r in revokes})
            # 先记下改动前的区间，再落库并取改动后的区间：并集覆盖
            # "旧区间 ∪ 新区间"，缩短/移动区间时旧位置上的样本也会重算。
            prior_all = _calibration_intervals(con, candidate_ids)
            touched = _apply_calibration_writes(con, writes, revokes)
            # 只有内容真的变化的 id 才进入影响范围；原样重交/重复撤销不重算
            prior_intervals = [x for x in prior_all if x[0] in set(touched)]
            post_intervals = _calibration_intervals(con, touched)
            intervals = _merge_intervals(prior_intervals + post_intervals)
            affected_ids = _readings_in_windows(con, intervals)
            rows = _result_rows(con, affected_ids)
            reading_changes = _write_results(con, rows)
            if _fault_hook is not None:
                # 此刻校准与明细结论已在同一事务中写入，小时尚未更新；
                # 回归测试在此真实杀进程，重开库应看不到任何一层的新值。
                _fault_hook("after_results_before_hours")
            hour_keys = _hours_for_readings(con, affected_ids)
            hour_changes = recompute_hours(con, hour_keys)
            # 有些小时原本会因这些读数退出而变空：上面的键集合已覆盖。
            run_id = _new_run_id(con, "calibration")
            run = _record_run(
                con,
                run_id=run_id,
                kind="apply_calibrations",
                payload_sha256=text_sha,
                reading_changes=reading_changes,
                hour_changes=hour_changes,
                extra={
                    "calibration_ids": touched,
                    "calibrations_written": [c.calibration_id for c in writes],
                    "calibrations_revoked": [r.calibration_id for r in revokes],
                    "affected_windows": [
                        {"sensor_id": s, "from": fmt_time(a), "to": fmt_time(b)}
                        for s, a, b in intervals
                    ],
                },
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return (
            {
                "status": "ok",
                "run_id": run_id,
                "calibration_ids": touched,
                "affected": run,
            },
            0,
        )
    except DomainError as ex:
        return {"status": "error", "errors": ex.errors}, ex.exit_code
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 对外入口：只读查询 / 全量重算（恢复核对用）


def status(db_path: str) -> tuple[dict, int]:
    con = connect(db_path)
    try:
        require_initialized(con)
        counts = {}
        for table in (
            "sensor_readings", "sensor_calibrations", "sensor_reading_results",
            "sensor_hourly", "sensor_processing_runs",
        ):
            counts[table] = con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
        by_reason = {
            row[0]: row[1]
            for row in con.execute(
                "SELECT reason, COUNT(*) FROM main.sensor_reading_results GROUP BY reason"
            ).fetchall()
        }
        return (
            {
                "status": "ok",
                "database": db_path,
                "counts": counts,
                "results_by_reason": dict(sorted(by_reason.items())),
                "schema_version": con.execute(
                    "SELECT value FROM _sensorpipe.meta WHERE key='schema_version'"
                ).fetchone()[0],
            },
            0,
        )
    except DomainError as ex:
        return {"status": "error", "errors": ex.errors}, ex.exit_code
    finally:
        con.close()


def query_detail(
    db_path: str,
    *,
    reading_id: str | None = None,
    sensor_id: str | None = None,
    status_filter: str | None = None,
    reason: str | None = None,
) -> dict:
    con = connect(db_path)
    try:
        require_initialized(con)
        clauses = []
        params: list[Any] = []
        if reading_id is not None:
            clauses.append("reading_id = ?")
            params.append(reading_id)
        if sensor_id is not None:
            clauses.append("sensor_id = ?")
            params.append(sensor_id)
        if status_filter is not None:
            clauses.append("status = ?")
            params.append(status_filter)
        if reason is not None:
            clauses.append("reason = ?")
            params.append(reason)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = con.execute(
            "SELECT reading_id, sensor_id, measured_at, hour_utc, "
            "temperature_original, battery, status, calibration_id, "
            "temperature_corrected, included, reason "
            f"FROM main.sensor_reading_results {where} "
            "ORDER BY sensor_id, measured_at, reading_id",
            params,
        ).fetchall()
        return {
            "status": "ok",
            "readings": [
                {
                    "reading_id": r[0],
                    "sensor_id": r[1],
                    "measured_at": fmt_time(r[2]),
                    "hour_utc": fmt_time(r[3].replace(tzinfo=UTC)),
                    "temperature_original": r[4],
                    "battery": r[5],
                    "status": r[6],
                    "calibration_id": r[7],
                    "temperature_corrected": r[8],
                    "included": r[9],
                    "reason": r[10],
                }
                for r in rows
            ],
        }
    finally:
        con.close()


def query_hourly(db_path: str, *, sensor_id: str | None = None) -> dict:
    con = connect(db_path)
    try:
        require_initialized(con)
        where = "WHERE sensor_id = ?" if sensor_id is not None else ""
        params = [sensor_id] if sensor_id is not None else []
        rows = con.execute(
            "SELECT sensor_id, hour_utc, samples, avg_temperature, min_battery, revision "
            f"FROM main.sensor_hourly {where} ORDER BY sensor_id, hour_utc",
            params,
        ).fetchall()
        return {
            "status": "ok",
            "hourly": [
                {
                    "sensor_id": r[0],
                    "hour_utc": fmt_time(r[1].replace(tzinfo=UTC)),
                    "samples": r[2],
                    "avg_temperature": r[3],
                    "min_battery": r[4],
                    "revision": r[5],
                }
                for r in rows
            ],
        }
    finally:
        con.close()


def query_runs(db_path: str, *, limit: int = 50) -> dict:
    con = connect(db_path)
    try:
        require_initialized(con)
        rows = con.execute(
            "SELECT run_id, kind, submitted_at, payload_sha256, readings_recomputed, "
            "readings_changed, hours_recomputed, hours_changed, hours_deleted, details_json "
            "FROM main.sensor_processing_runs ORDER BY submitted_at DESC, run_id DESC "
            "LIMIT ?",
            [max(1, int(limit))],
        ).fetchall()
        return {
            "status": "ok",
            "runs": [
                {
                    "run_id": r[0],
                    "kind": r[1],
                    "submitted_at": fmt_time(r[2]),
                    "payload_sha256": r[3],
                    "readings_recomputed": r[4],
                    "readings_changed": r[5],
                    "hours_recomputed": r[6],
                    "hours_changed": r[7],
                    "hours_deleted": r[8],
                    "details": json.loads(r[9]),
                }
                for r in rows
            ],
        }
    finally:
        con.close()


def rebuild_all(db_path: str) -> tuple[dict, int]:
    """从原始读数 + 校准全量重算结论与小时（恢复工具，不是业务输入入口）。"""
    con = connect(db_path)
    try:
        require_initialized(con)
        con.execute("BEGIN TRANSACTION")
        try:
            rows = _result_rows(con, None)
            reading_changes = _write_results(con, rows)
            hour_keys = _hours_for_readings(con, [r[0] for r in rows])
            # 还要包含当前库里可能有、但已没有任何读数的陈旧小时组
            for row in con.execute(
                "SELECT sensor_id, hour_utc FROM main.sensor_hourly"
            ).fetchall():
                hour_keys.append((row[0], row[1]))
            hour_changes = recompute_hours(con, hour_keys)
            run_id = _new_run_id(con, "rebuild")
            run = _record_run(
                con,
                run_id=run_id,
                kind="rebuild_all",
                payload_sha256="sha256:rebuild",
                reading_changes=reading_changes,
                hour_changes=hour_changes,
                extra={},
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return {"status": "ok", "run_id": run_id, "affected": run}, 0
    except DomainError as ex:
        return {"status": "error", "errors": ex.errors}, ex.exit_code
    finally:
        con.close()
