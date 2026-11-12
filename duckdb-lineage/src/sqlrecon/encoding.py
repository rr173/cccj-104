"""把 DuckDB 返回的 Python 值编码为可恢复类型与值的 JSON 表达。

普通 JSON 数字会丢两件事：

1. 精确整数（尤其 HUGEINT 大整数、DECIMAL）被解析成 float 后可能丢精度；
2. NaN / +Inf / -Inf 不是合法 JSON 标记。

因此编码规则是"每个标量一个对象"，键 ``t`` 是 DuckDB 逻辑类型族，``v`` 的
表达保证能反推出原始类型与值：

==================  ============================================================
类型族 (t)          表达 (v)
==================  ============================================================
null                None（独立 null 标量；行内的 NULL 同样是 {"t":"null"}）
boolean             true / false
integer             整数字面量（Python int 原样输出，不经过 float）
decimal             十进制小数字符串（Decimal 的 ascii 表达，保留全部位数）
float               有限数 -> JSON number；NaN/±Inf -> "NaN"/"Infinity"/
                    "-Infinity" 字符串标记
varchar             字符串
blob                base64 字符串
date                "YYYY-MM-DD"
time                "HH:MM:SS[.ffffff]"
timestamp           "YYYY-MM-DDTHH:MM:SS[.ffffff]"（无时区墙上时间）
timestamp_tz        "YYYY-MM-DDTHH:MM:SS[.ffffff]+HH:MM"（带偏移）
interval            ISO-8601 风格时长字符串（timedelta 表达）
uuid                UUID 规范字符串
bit                 位串
list/array          同构编码后的数组
struct/map          字段/键值数组（嵌套值按同规则递归编码）
其他                 {"t": 类型名字符串, "v": str(value)} 保底，不静默转字符串
==================  ============================================================

报告里每一行同时给出列名，因此阅读时类型 + 列定义可完整恢复原值；数值
绝不经过 float 重新序列化。
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import math
import uuid as _uuid
from typing import Any

# DuckDB 逻辑类型 id（DuckDBPyType.id）到类型族标签。
# 整数族一律走 Python int，HUGEINT/UTINYINT 等不会被 float 截断。
_INTEGER_IDS = {
    "tinyint", "smallint", "integer", "bigint", "hugeint",
    "utinyint", "usmallint", "uinteger", "ubigint",
}
_FLOAT_IDS = {"float", "double"}
_DECIMAL_ID = "decimal"


class EncodeError(Exception):
    """遇到无法稳定编码的值（理论上不应发生，保底不静默丢精度）。"""


def encode_scalar(value: Any, type_id: str | None = None) -> dict[str, Any]:
    """把单个值编码为 {"t": ..., "v": ...}。

    ``type_id`` 是该列的 DuckDB 逻辑类型 id；为 None 时按 Python 类型推断，
    仅用于嵌套容器（list/struct/map 的元素类型由 DuckDB 复合类型推断）。
    """
    if value is None:
        return {"t": "null", "v": None}

    # --- 精确整数：任何路径都不经过 float ---
    if isinstance(value, bool):
        return {"t": "boolean", "v": value}
    if isinstance(value, int):
        return {"t": "integer", "v": value}
    if isinstance(value, decimal.Decimal):
        # Decimal 可能是 sNaN/NaN 或 ±Infinity（DuckDB DECIMAL 本身不产生，
        # 但 Python 侧手工构造或未来扩展时仍按自描述规则编码）。
        if value.is_nan():
            return {"t": "decimal", "v": "NaN"}
        if value.is_infinite():
            return {"t": "decimal", "v": "Infinity" if value > 0 else "-Infinity"}
        return {"t": "decimal", "v": format(value, "f")}
    if isinstance(value, float):
        if math.isnan(value):
            return {"t": "float", "v": "NaN"}
        if math.isinf(value):
            return {"t": "float", "v": "Infinity" if value > 0 else "-Infinity"}
        return {"t": "float", "v": value}
    if isinstance(value, str):
        # BIT 在 DuckDB Python 侧以字符串形式返回（如 "1010"），
        # 由列类型 id 区分；这里处理普通 varchar。
        if type_id == "bit":
            return {"t": "bit", "v": value}
        return {"t": "varchar", "v": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "t": "blob",
            "v": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, _dt.datetime):
        return {"t": _timestamp_tag(type_id, value), "v": _format_datetime(value)}
    if isinstance(value, _dt.date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, _dt.time):
        return {"t": "time", "v": _format_time(value)}
    if isinstance(value, _dt.timedelta):
        return {"t": "interval", "v": _format_interval(value)}
    if isinstance(value, _uuid.UUID):
        return {"t": "uuid", "v": str(value)}
    if isinstance(value, list):
        return {"t": "list", "v": [encode_scalar(item) for item in value]}
    if isinstance(value, tuple):
        return {"t": "list", "v": [encode_scalar(item) for item in value]}
    if isinstance(value, dict):
        return {
            "t": "map",
            "v": [
                {"key": encode_scalar(k), "value": encode_scalar(val)}
                for k, val in sorted(value.items(), key=lambda kv: str(kv[0]))
            ],
        }
    # 保底：绝不隐式 str() 后冒充字符串相等，明确标注真实 Python 类型。
    return {"t": f"python:{type(value).__name__}", "v": str(value)}


def _timestamp_tag(type_id: str | None, value: _dt.datetime) -> str:
    # DuckDB 的 TIMESTAMPTZ 逻辑类型 id 是 "timestamp with time zone"（含空格）。
    if (
        type_id == "timestamptz"
        or (isinstance(type_id, str) and "time zone" in type_id)
        or value.tzinfo is not None
    ):
        return "timestamp_tz"
    return "timestamp"


def _format_datetime(value: _dt.datetime) -> str:
    text = value.isoformat(sep="T")
    # Python 对 +00:00 与秒精度的输出已稳定；统一不做裁剪，保留微秒。
    return text


def _format_time(value: _dt.time) -> str:
    return value.isoformat()


def _format_interval(value: _dt.timedelta) -> str:
    """把 timedelta 编为自描述 ISO-8601 时长，如 PT01H02M03.000004S / P-1DT...。"""
    total_seconds = value.total_seconds()
    sign = "-" if total_seconds < 0 else ""
    absorbed = abs(total_seconds)
    days, rem = divmod(absorbed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    second_text = f"{seconds:09.6f}" if seconds else "00"
    return (
        f"P{sign}{int(days)}DT{sign}{int(hours):02d}H"
        f"{sign}{int(minutes):02d}M{sign}{second_text}S"
    )


def encode_row(row: tuple[Any, ...], type_ids: tuple[str | None, ...]) -> list[dict[str, Any]]:
    """整行编码：逐列附带该列 DuckDB 逻辑类型 id。"""
    return [encode_scalar(v, tid) for v, tid in zip(row, type_ids)]
