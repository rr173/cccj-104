"""时间、数值与序列化的小工具（纯函数，不接触数据库）。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .errors import err

UTC = timezone.utc


def parse_timezone_aware(value: Any, *, field: str, line: int | None = None) -> datetime:
    """解析必须带时区的 ISO-8601 时刻，统一换算成 UTC 再返回。"""
    extra = {"field": field}
    if line is not None:
        extra["line"] = line
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空 ISO-8601 字符串（含时区），收到 {value!r}")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as ex:
        raise ValueError(f"{field} 不是合法 ISO-8601 时刻: {value!r}（{ex}）") from ex
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{field} 缺少时区: {value!r}；例如 2026-03-01T10:00:00Z 或 "
            "2026-03-01T18:00:00+08:00"
        )
    return parsed.astimezone(UTC)


def parse_finite_number(value: Any, *, field: str, line: int | None = None) -> float:
    """接受 JSON 整数/小数（拒绝 bool），要求有限数值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数值，收到 {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数值，收到 {value!r}")
    return number


def to_naive_utc(moment: datetime) -> datetime:
    """把带时区时刻转成"UTC 墙上时间"的 naive datetime（落库形式）。"""
    return moment.astimezone(UTC).replace(tzinfo=None)


def fmt_time(value: datetime | None) -> str | None:
    """统一输出成 UTC 的 ISO-8601（带 Z）；naive 值按 UTC 处理。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    base = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        base += f".{value.microsecond:06d}"
    return base + "Z"


def make_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return err(code, message, **extra)
