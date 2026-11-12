"""检查清单加载与规则校验。

清单是一个 JSON 文件，描述一组命名检查项。每项给出旧查询、新查询和比较
规则；查询既可以内联在 ``old_sql`` / ``new_sql`` 字段里，也可以用
``old_sql_file`` / ``new_sql_file`` 指向相对清单目录的 .sql 文件。

清单格式::

    {
      "checks": [
        {
          "name": "hourly_rewrite",
          "description": "嵌套子查询 -> JOIN 改写（可选）",
          "keys": ["sensor_id", "hour_utc"],
          "old_sql_file": "queries/01_old.sql",
          "new_sql_file": "queries/01_new.sql",
          "tolerance": [
            {"column": "avg_temperature", "abs": 0.0001, "rel": 0.000001}
          ]
        }
      ]
    }

规则：

* ``name`` 必填且清单内唯一；``keys`` 可省略或给空数组，表示不分组、
  整张结果当一个组；
* 键列始终精确比较，容差只作用于非键数值列；
* ``abs`` / ``rel`` 必须是有限非负数（JSON number，不接受 NaN/Inf/字符串）；
* 同一列最多声明一条容差；容差列与键列重叠、容差列重名都在加载期拒绝；
* 列名是否真的存在于查询输出，要等执行后拿到列描述才能判定，由
  :mod:`sqlrecon.compare` 负责报 ``schema_*`` 错误。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ManifestError


@dataclass(frozen=True)
class Tolerance:
    """单个数值列的容差：|diff| <= abs + rel * max(|x|, |y|)。"""

    column: str
    abs: float
    rel: float


@dataclass
class CheckSpec:
    """一项命名的改写对账检查。"""

    name: str
    old_sql: str
    new_sql: str
    keys: tuple[str, ...] = ()
    tolerance: dict[str, Tolerance] = field(default_factory=dict)
    description: str = ""
    source: str = ""  # 清单内定位用（文件名/序号），错误报告附带

    @property
    def grouped(self) -> bool:
        return bool(self.keys)


def _require_object(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} 必须是 JSON 对象，实际是 {type(raw).__name__}")
    return raw


def _finite_non_negative(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(
            f"{where} 必须是 JSON 数值（有限、非负），实际是 {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ManifestError(f"{where} 必须是有限数，不能是 NaN/Infinity")
    if number < 0:
        raise ManifestError(f"{where} 必须是非负数，收到 {value}")
    return number


def _load_sql_text(base_dir: Path, raw: dict[str, Any], side: str) -> str:
    inline = raw.get(f"{side}_sql")
    file_ref = raw.get(f"{side}_sql_file")
    if inline is not None and file_ref is not None:
        raise ManifestError(
            f"检查项不能同时给出 {side}_sql 与 {side}_sql_file，请二选一"
        )
    if inline is not None:
        if not isinstance(inline, str) or not inline.strip():
            raise ManifestError(f"{side}_sql 必须是非空字符串")
        return inline
    if file_ref is not None:
        if not isinstance(file_ref, str) or not file_ref:
            raise ManifestError(f"{side}_sql_file 必须是非空字符串路径")
        path = (base_dir / file_ref).resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as ex:
            raise ManifestError(f"无法读取 {side}_sql_file: {path} ({ex})") from ex
        if not text.strip():
            raise ManifestError(f"{side}_sql_file 内容为空: {path}")
        return text
    raise ManifestError(f"检查项缺少 {side}_sql 或 {side}_sql_file")


def _parse_tolerance(raw_tol: Any, keys: tuple[str, ...], where: str) -> dict[str, Tolerance]:
    if raw_tol is None:
        return {}
    if not isinstance(raw_tol, list):
        raise ManifestError(f"{where}.tolerance 必须是数组")
    out: dict[str, Tolerance] = {}
    for idx, item in enumerate(raw_tol):
        spot = f"{where}.tolerance[{idx}]"
        obj = _require_object(item, spot)
        col = obj.get("column")
        if not isinstance(col, str) or not col:
            raise ManifestError(f"{spot}.column 必须是非空列名字符串")
        if col in out:
            raise ManifestError(f"{spot}: 列 '{col}' 的容差重复声明")
        if col in keys:
            raise ManifestError(
                f"{spot}: 键列 '{col}' 必须精确比较，不能声明容差"
            )
        abs_v = _finite_non_negative(obj.get("abs", 0.0), f"{spot}.abs")
        rel_v = _finite_non_negative(obj.get("rel", 0.0), f"{spot}.rel")
        out[col] = Tolerance(column=col, abs=abs_v, rel=rel_v)
    return out


def load_manifest(path: str | Path) -> list[CheckSpec]:
    """加载并校验清单，返回检查项列表（保持清单书写顺序）。"""
    manifest_path = Path(path)
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as ex:
        raise ManifestError(f"无法读取清单文件 {manifest_path}: {ex}") from ex
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as ex:
        raise ManifestError(f"清单不是合法 JSON: {ex}") from ex

    root = _require_object(raw, "清单根对象")
    checks_raw = root.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise ManifestError("清单必须包含非空数组字段 'checks'")

    base_dir = manifest_path.parent
    specs: list[CheckSpec] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(checks_raw):
        where = f"checks[{idx}]"
        obj = _require_object(item, where)
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{where}.name 必须是非空字符串")
        if name in seen_names:
            raise ManifestError(f"检查项名称重复: '{name}'")
        seen_names.add(name)

        raw_keys = obj.get("keys", [])
        if raw_keys is None:
            raw_keys = []
        if not isinstance(raw_keys, list) or not all(
            isinstance(k, str) and k for k in raw_keys
        ):
            raise ManifestError(f"{where}.keys 必须是列名字符串数组（可省略）")
        if len(set(raw_keys)) != len(raw_keys):
            raise ManifestError(f"{where}.keys 存在重复键列: {raw_keys}")
        keys = tuple(raw_keys)

        tolerance = _parse_tolerance(obj.get("tolerance"), keys, where)
        old_sql = _load_sql_text(base_dir, obj, "old")
        new_sql = _load_sql_text(base_dir, obj, "new")

        desc = obj.get("description", "")
        if desc is None:
            desc = ""
        if not isinstance(desc, str):
            raise ManifestError(f"{where}.description 必须是字符串")

        specs.append(
            CheckSpec(
                name=name,
                old_sql=old_sql,
                new_sql=new_sql,
                keys=keys,
                tolerance=tolerance,
                description=desc,
                source=manifest_path.name,
            )
        )
    return specs
