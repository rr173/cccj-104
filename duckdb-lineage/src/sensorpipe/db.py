"""库结构、连接与初始化。

所有业务表都在 ``main`` schema。读数表列名沿用演示：
``sensor_id / measured_at / temperature / battery / status``，只多了主键
``reading_id`` 与内容指纹；``temperature`` 永远保存原始值，不会被修正温度覆盖。

  sensor_readings         原始读数（temperature=原始温度，content_sha256=规范化内容指纹）
  sensor_calibrations     校准记录；生效区间 [valid_from, valid_to)，同设备生效区间不相交
  sensor_reading_results  每条读数的处理结论（命中校准/修正温度/纳入与否/原因）
  sensor_hourly           纳入指标的小时结果（设备 + UTC 小时）；空组删除
  sensor_processing_runs  每次成功处理的影响范围摘要
  sensor_run_readings     本次重新计算的读数（before/after）
  sensor_run_hours        本次重新计算的小时（before/after，含删除）

revision 列的用途：仅当该行业务内容实际变化时 +1。回归测试可用它检查
"无关记录没有被改写"，而不只是比较最终数值。

时刻在库里一律保存为 **UTC 墙上时间的 TIMESTAMP（无时区）**。输入必须带
时区，解析后立即换算为 UTC 再落库；DuckDB 的 TIMESTAMP 比较、date_trunc
与时区设置完全无关，因此结果不受运行机器/会话时区影响，也不依赖 pytz
等时区库。对外输出统一补 ``Z``。
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .errors import DomainError

SCHEMA_VERSION = "1"

DDL = [
    "CREATE SCHEMA IF NOT EXISTS main",
    "CREATE SCHEMA IF NOT EXISTS _sensorpipe",
    """
    CREATE TABLE IF NOT EXISTS _sensorpipe.meta (
        key VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS main.sensor_readings (
        reading_id VARCHAR PRIMARY KEY,
        sensor_id VARCHAR NOT NULL,
        measured_at TIMESTAMP NOT NULL,
        temperature DOUBLE NOT NULL,
        battery DOUBLE NOT NULL,
        status VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        revision BIGINT NOT NULL DEFAULT 1,
        CHECK (isfinite(temperature)),
        CHECK (isfinite(battery))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS main.sensor_calibrations (
        calibration_id VARCHAR PRIMARY KEY,
        sensor_id VARCHAR NOT NULL,
        valid_from TIMESTAMP NOT NULL,
        valid_to TIMESTAMP NOT NULL,
        gain DOUBLE NOT NULL,
        offset_value DOUBLE NOT NULL,
        revoked BOOLEAN NOT NULL DEFAULT FALSE,
        revision BIGINT NOT NULL DEFAULT 1,
        CHECK (valid_to > valid_from),
        CHECK (isfinite(gain)),
        CHECK (isfinite(offset_value))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS main.sensor_reading_results (
        reading_id VARCHAR PRIMARY KEY,
        sensor_id VARCHAR NOT NULL,
        measured_at TIMESTAMP NOT NULL,
        hour_utc TIMESTAMP NOT NULL,
        temperature_original DOUBLE NOT NULL,
        battery DOUBLE NOT NULL,
        status VARCHAR NOT NULL,
        calibration_id VARCHAR,
        temperature_corrected DOUBLE,
        included BOOLEAN NOT NULL,
        reason VARCHAR NOT NULL,
        revision BIGINT NOT NULL DEFAULT 1,
        CHECK (reason IN ('included', 'fault', 'missing_calibration', 'out_of_range')),
        CHECK (NOT included OR (status = 'OK' AND calibration_id IS NOT NULL
               AND temperature_corrected BETWEEN -50.0 AND 80.0))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS main.sensor_hourly (
        sensor_id VARCHAR NOT NULL,
        hour_utc TIMESTAMP NOT NULL,
        samples BIGINT NOT NULL,
        avg_temperature DOUBLE NOT NULL,
        min_battery DOUBLE NOT NULL,
        revision BIGINT NOT NULL DEFAULT 1,
        PRIMARY KEY (sensor_id, hour_utc),
        CHECK (samples > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS main.sensor_processing_runs (
        run_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL,
        submitted_at TIMESTAMP NOT NULL,
        payload_sha256 VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        readings_recomputed INTEGER NOT NULL,
        readings_changed INTEGER NOT NULL,
        hours_recomputed INTEGER NOT NULL,
        hours_changed INTEGER NOT NULL,
        hours_deleted INTEGER NOT NULL,
        details_json VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS main.sensor_run_readings (
        run_id VARCHAR NOT NULL,
        reading_id VARCHAR NOT NULL,
        calibration_before VARCHAR,
        calibration_after VARCHAR,
        corrected_before DOUBLE,
        corrected_after DOUBLE,
        reason_before VARCHAR,
        reason_after VARCHAR,
        included_before BOOLEAN,
        included_after BOOLEAN,
        changed BOOLEAN NOT NULL,
        PRIMARY KEY (run_id, reading_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS main.sensor_run_hours (
        run_id VARCHAR NOT NULL,
        sensor_id VARCHAR NOT NULL,
        hour_utc TIMESTAMP NOT NULL,
        change VARCHAR NOT NULL,
        samples_before BIGINT,
        samples_after BIGINT,
        avg_before DOUBLE,
        avg_after DOUBLE,
        min_battery_before DOUBLE,
        min_battery_after DOUBLE,
        PRIMARY KEY (run_id, sensor_id, hour_utc),
        CHECK (change IN ('created', 'updated', 'deleted', 'unchanged'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sensor_readings_sensor_time
        ON main.sensor_readings (sensor_id, measured_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sensor_results_sensor_time
        ON main.sensor_reading_results (sensor_id, measured_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sensor_calibrations_sensor
        ON main.sensor_calibrations (sensor_id, valid_from, valid_to)
    """,
]


REQUIRED_COLUMNS = {
    "sensor_readings": {
        "reading_id", "sensor_id", "measured_at", "temperature",
        "battery", "status", "content_sha256", "revision",
    },
    "sensor_calibrations": {
        "calibration_id", "sensor_id", "valid_from", "valid_to",
        "gain", "offset_value", "revoked", "revision",
    },
    "sensor_reading_results": {
        "reading_id", "sensor_id", "measured_at", "hour_utc",
        "temperature_original", "battery", "status", "calibration_id",
        "temperature_corrected", "included", "reason", "revision",
    },
    "sensor_hourly": {
        "sensor_id", "hour_utc", "samples", "avg_temperature",
        "min_battery", "revision",
    },
    "sensor_processing_runs": {
        "run_id", "kind", "submitted_at", "payload_sha256", "status",
        "readings_recomputed", "readings_changed", "hours_recomputed",
        "hours_changed", "hours_deleted", "details_json",
    },
    "sensor_run_readings": {
        "run_id", "reading_id", "calibration_before", "calibration_after",
        "corrected_before", "corrected_after", "reason_before",
        "reason_after", "included_before", "included_after", "changed",
    },
    "sensor_run_hours": {
        "run_id", "sensor_id", "hour_utc", "change", "samples_before",
        "samples_after", "avg_before", "avg_after", "min_battery_before",
        "min_battery_after",
    },
}


def connect(db_path: str | Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """打开库文件。库里全是 UTC 墙上时间 TIMESTAMP，不依赖会话时区。"""
    return duckdb.connect(str(db_path), read_only=read_only)


def _main_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0].lower()
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = 'main'"
        ).fetchall()
    }


def table_columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    rows = con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE database_name = current_database() AND schema_name = 'main' "
        "AND table_name = ?",
        [table],
    ).fetchall()
    return {row[0].lower() for row in rows}


def initialize(con: duckdb.DuckDBPyConnection) -> None:
    """幂等建表；若同名表结构不同则明确拒绝，避免两套模型混用。"""
    existing = _main_tables(con)
    for table, required in REQUIRED_COLUMNS.items():
        if table not in existing:
            continue
        missing = sorted(required - table_columns(con, table))
        if missing:
            raise DomainError.one(
                "incompatible_schema",
                f"库内已有 main.{table} 但缺少本处理链所需列: {missing}；"
                "请使用独立库文件，或先迁移旧表。",
                table=f"main.{table}",
                missing=missing,
            )
    for statement in DDL:
        con.execute(statement)
    con.execute(
        "INSERT INTO _sensorpipe.meta VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [SCHEMA_VERSION],
    )


def require_initialized(con: duckdb.DuckDBPyConnection) -> None:
    tables = _main_tables(con)
    needed = set(REQUIRED_COLUMNS)
    if not needed <= tables:
        raise DomainError.one(
            "not_initialized",
            "目标库尚未初始化（缺少 sensorpipe 表）；请先运行 init --db",
            missing=sorted(needed - tables),
        )
    for table, required in REQUIRED_COLUMNS.items():
        missing = sorted(required - table_columns(con, table))
        if missing:
            raise DomainError.one(
                "incompatible_schema",
                f"main.{table} 缺少列: {missing}",
                table=f"main.{table}",
                missing=missing,
            )


def init_db(db_path: str | Path) -> dict:
    path = Path(db_path)
    con = connect(path)
    try:
        initialize(con)
        return {
            "status": "ok",
            "database": str(path),
            "schema_version": SCHEMA_VERSION,
        }
    finally:
        con.close()
