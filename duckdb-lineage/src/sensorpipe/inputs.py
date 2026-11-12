"""输入解析：JSON Lines 读数与校准命令（纯函数，便于逐行报错）。

读数行（字段名沿用演示，另加调用方提供的 reading_id）：

    {"reading_id": "r1", "sensor_id": "s1",
     "measured_at": "2026-03-01T10:05:00Z",
     "temperature": 21.5, "battery": 90.0, "status": "OK"}

校准行（新增或修改/恢复）：

    {"calibration_id": "c1", "sensor_id": "s1",
     "valid_from": "2026-03-01T10:00:00Z",
     "valid_to": "2026-03-01T11:00:00Z",
     "gain": 1.0, "offset": -0.5}

撤销行：

    {"calibration_id": "c1", "revoke": true}

时刻必须带时区；统一换算为 UTC。数值必须是有限数值；校准 valid_to 必须
晚于 valid_from。status 只要求非空字符串，落库规范化为大写；非 OK 的读数
保留但不进指标（演示数据用 FAULT 表示故障）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, FrozenSet

from .util import parse_finite_number, parse_timezone_aware

READING_FIELDS = {
    "reading_id",
    "sensor_id",
    "measured_at",
    "temperature",
    "battery",
    "status",
}
CALIBRATION_FIELDS = {
    "calibration_id",
    "sensor_id",
    "valid_from",
    "valid_to",
    "gain",
    "offset",
}
REVOKE_FIELDS = {"calibration_id", "revoke"}


@dataclass(frozen=True)
class Reading:
    reading_id: str
    sensor_id: str
    measured_at: Any  # datetime，UTC
    temperature: float
    battery: float
    status: str

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "reading_id": self.reading_id,
                "sensor_id": self.sensor_id,
                "measured_at": self.measured_at.isoformat(),
                "temperature": self.temperature,
                "battery": self.battery,
                "status": self.status,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class Calibration:
    calibration_id: str
    sensor_id: str
    valid_from: Any  # datetime，UTC
    valid_to: Any  # datetime，UTC
    gain: float
    offset: float

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "calibration_id": self.calibration_id,
                "sensor_id": self.sensor_id,
                "valid_from": self.valid_from.isoformat(),
                "valid_to": self.valid_to.isoformat(),
                "gain": self.gain,
                "offset": self.offset,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Revocation:
    calibration_id: str


CalibrationOp = Calibration | Revocation


def _row_error(line_no: int, message: str, **extra: Any) -> dict[str, Any]:
    payload = {"code": "input_error", "line": line_no, "message": message}
    payload.update(extra)
    return payload


def _nonempty_str(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} 必须是非空字符串，收到 {raw!r}")
    return raw.strip()


def _parse_line_object(line: str, line_no: int) -> dict[str, Any]:
    try:
        obj = json.loads(line)
    except ValueError as ex:
        raise ValueError(f"不是合法 JSON: {ex}") from ex
    if not isinstance(obj, dict):
        raise ValueError("每行必须是 JSON 对象")
    return obj


def _reject_extra_fields(obj: dict[str, Any], allowed: FrozenSet[str]) -> None:
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise ValueError(f"包含未知字段: {extra}（允许字段 {sorted(allowed)}）")


def parse_reading_line(line: str, line_no: int) -> Reading:
    obj = _parse_line_object(line, line_no)
    _reject_extra_fields(obj, READING_FIELDS)
    missing = sorted(READING_FIELDS - set(obj))
    if missing:
        raise ValueError(f"缺少字段: {missing}")
    reading_id = _nonempty_str(obj["reading_id"], "reading_id")
    sensor_id = _nonempty_str(obj["sensor_id"], "sensor_id")
    measured_at = parse_timezone_aware(obj["measured_at"], field="measured_at", line=line_no)
    temperature = parse_finite_number(obj["temperature"], field="temperature", line=line_no)
    battery = parse_finite_number(obj["battery"], field="battery", line=line_no)
    status_raw = obj["status"]
    if not isinstance(status_raw, str) or not status_raw.strip():
        raise ValueError(f"status 必须是非空字符串，收到 {status_raw!r}")
    return Reading(
        reading_id=reading_id,
        sensor_id=sensor_id,
        measured_at=measured_at,
        temperature=temperature,
        battery=battery,
        status=status_raw.strip().upper(),
    )


def parse_calibration_line(line: str, line_no: int) -> CalibrationOp:
    obj = _parse_line_object(line, line_no)
    if "revoke" in obj:
        if obj.get("revoke") is not True:
            raise ValueError("revoke 只能为 true（撤销用单独一行）,收到其它值")
        _reject_extra_fields(obj, REVOKE_FIELDS)
        missing = sorted(REVOKE_FIELDS - set(obj))
        if missing:
            raise ValueError(f"缺少字段: {missing}")
        return Revocation(
            calibration_id=_nonempty_str(obj["calibration_id"], "calibration_id")
        )
    _reject_extra_fields(obj, CALIBRATION_FIELDS)
    missing = sorted(CALIBRATION_FIELDS - set(obj))
    if missing:
        raise ValueError(f"缺少字段: {missing}")
    calibration_id = _nonempty_str(obj["calibration_id"], "calibration_id")
    sensor_id = _nonempty_str(obj["sensor_id"], "sensor_id")
    valid_from = parse_timezone_aware(obj["valid_from"], field="valid_from", line=line_no)
    valid_to = parse_timezone_aware(obj["valid_to"], field="valid_to", line=line_no)
    if valid_to <= valid_from:
        raise ValueError(
            f"valid_to 必须晚于 valid_from（收到 {obj['valid_from']!r} -> {obj['valid_to']!r}）"
        )
    gain = parse_finite_number(obj["gain"], field="gain", line=line_no)
    offset = parse_finite_number(obj["offset"], field="offset", line=line_no)
    return Calibration(
        calibration_id=calibration_id,
        sensor_id=sensor_id,
        valid_from=valid_from,
        valid_to=valid_to,
        gain=gain,
        offset=offset,
    )


def parse_lines(
    text: str, parser: Any, *, source: str
) -> tuple[list[Any], list[dict[str, Any]], str]:
    """逐行解析；空行跳过。返回 (对象, 错误, 规范化原文的 sha256)。

    指纹基于原始文本，用于幂等重传判定：同一文件原样重传 -> 指纹相同。
    """
    records: list[Any] = []
    errors: list[dict[str, Any]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            records.append(parser(raw, line_no))
        except ValueError as ex:
            errors.append(_row_error(line_no, str(ex), source=source))
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return records, errors, "sha256:" + sha


def read_payload(
    *, file: str | None, stdin_text: str | None, source_label: str
) -> tuple[str, str]:
    """从文件或 stdin 读取原文；返回 (文本, 来源标签)。"""
    if file is not None and stdin_text is not None:
        raise ValueError("不能同时从 --file 与 stdin 读取")
    if file is not None:
        with open(file, "r", encoding="utf-8") as fh:
            return fh.read(), str(file)
    if stdin_text is None:
        raise ValueError("缺少输入：请使用 --file PATH 或从 stdin 传入 JSONL")
    return stdin_text, source_label
