"""资产清单加载：声明本工具负责维护的视图名及其查询文件。

资产目录布局：

    assets/
    ├── manifest.json       # 清单
    └── queries/*.sql       # 每个视图一条 SELECT

清单格式（JSON）：

    {
      "schema": "main",                 // 可选，视图落库 schema，默认 "main"
      "views": {                        // 必填；视图名 -> 查询文件（相对清单目录）
        "sensor_clean": "queries/10_sensor_clean.sql",
        "analytics.sensor_hourly": "queries/20_sensor_hourly.sql"
      }
    }

规则：

  * 视图名大小写不敏感，规范化为小写 ``schema.name`` 作为身份；
  * 清单内（含 schema 限定后）不允许重名；
  * 保留 schema ``_viewctl`` 供注册表使用，不能作为视图落点；
  * 每个查询文件必须恰好是一条 SELECT（可含 CTE / UNION ALL），不允许 DDL/DML；
  * 清单书写顺序不代表执行顺序，执行顺序由视图间引用关系决定；
  * ``views`` 允许为空对象：表示本版本撤下全部已登记视图。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import err
from .refs import QueryShapeError, extract_refs, parse_query

MANIFEST_NAME = "manifest.json"
REGISTRY_SCHEMA = "_viewctl"
DEFAULT_SCHEMA = "main"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Asset:
    """一个候选视图：规范身份 + 查询文本 + 引用集合。"""

    name: str  # 规范化 schema.name（小写）
    schema_name: str
    view_name: str
    path: str  # 查询文件绝对路径（用于错误报告）
    rel_path: str  # 清单中声明的相对路径
    sql_text: str
    sha256: str
    refs: set[str] = field(default_factory=set)


@dataclass
class Bundle:
    """一次候选发布：清单声明的全部资产 + 源指纹。"""

    assets_dir: str
    schema: str  # 清单默认 schema
    assets: dict[str, Asset] = field(default_factory=dict)
    digest: str = ""


def _split_view_name(raw: str) -> tuple[str, str] | None:
    """把清单视图名拆成 (schema, view)；不合法返回 None。"""
    parts = raw.split(".")
    if len(parts) == 1:
        return "", parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _dedupe_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook：JSON 对象键重复时直接报错（不静默取后者）。"""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"JSON 对象键重复: {key!r}")
        out[key] = value
    return out


def load_manifest(assets_dir: str | Path) -> tuple[dict[str, Any] | None, list[dict]]:
    assets_path = Path(assets_dir)
    manifest_path = assets_path / MANIFEST_NAME
    if not manifest_path.is_file():
        return None, [
            err(
                "missing_resource",
                f"资产目录缺少清单文件: {manifest_path}",
                path=str(manifest_path),
            )
        ]
    try:
        data = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_dedupe_json,
        )
    except (ValueError, OSError) as ex:
        return None, [
            err(
                "manifest_error",
                f"清单解析失败: {ex}",
                path=str(manifest_path),
            )
        ]
    if not isinstance(data, dict):
        return None, [err("manifest_error", "清单顶层必须是 JSON 对象")]
    return data, []


def load_bundle(
    assets_dir: str | Path,
    current_database: str | None = None,
) -> tuple[Bundle | None, list[dict]]:
    """加载清单与全部查询文件，返回候选包或结构化错误列表。"""
    assets_path = Path(assets_dir).resolve()
    data, errors = load_manifest(assets_path)
    if errors:
        return None, errors
    assert data is not None

    schema = data.get("schema", DEFAULT_SCHEMA)
    if not isinstance(schema, str) or not _IDENT.match(schema):
        return None, [
            err("manifest_error", f"清单 schema 非法: {schema!r}（须为标识符）")
        ]
    schema = schema.lower()
    if schema == REGISTRY_SCHEMA:
        return None, [
            err(
                "manifest_error",
                f"schema {REGISTRY_SCHEMA!r} 是注册表保留名，不能作为视图落点",
            )
        ]

    views = data.get("views")
    if not isinstance(views, dict):
        return None, [
            err("manifest_error", "清单缺少 views 对象（视图名 -> 查询文件）")
        ]

    bundle = Bundle(assets_dir=str(assets_path), schema=schema)
    errors: list[dict] = []
    seen: dict[str, str] = {}  # 规范名 -> 原始声明名（重名报告用）

    for raw_name, rel in views.items():
        if not isinstance(raw_name, str) or not isinstance(rel, str):
            errors.append(
                err(
                    "manifest_error",
                    f"views 条目须为 字符串视图名: 字符串路径，收到 {raw_name!r}: {rel!r}",
                )
            )
            continue
        parts = _split_view_name(raw_name)
        if parts is None:
            errors.append(
                err(
                    "manifest_error",
                    f"视图名非法: {raw_name!r}（须为 name 或 schema.name）",
                )
            )
            continue
        schema_part, view_part = parts
        target_schema = (schema_part or schema).lower()
        view_name = view_part.lower()
        if not _IDENT.match(view_part) or (schema_part and not _IDENT.match(schema_part)):
            errors.append(
                err("manifest_error", f"视图名含非法标识符: {raw_name!r}")
            )
            continue
        if target_schema == REGISTRY_SCHEMA:
            errors.append(
                err(
                    "manifest_error",
                    f"视图 {raw_name!r} 落在注册表保留 schema {REGISTRY_SCHEMA!r}",
                )
            )
            continue
        norm = f"{target_schema}.{view_name}"
        if norm in seen:
            errors.append(
                err(
                    "duplicate_name",
                    f"清单重名: {raw_name!r} 与 {seen[norm]!r} 规范化为同一对象 {norm}",
                    name=norm,
                )
            )
            continue
        seen[norm] = raw_name

        file_path = (assets_path / rel).resolve()
        try:
            file_path.relative_to(assets_path)
        except ValueError:
            errors.append(
                err(
                    "missing_resource",
                    f"查询文件必须位于资产目录内: {rel!r}",
                    asset=norm,
                )
            )
            continue
        if not file_path.is_file():
            errors.append(
                err(
                    "missing_resource",
                    f"清单声明的查询文件不存在: {rel}",
                    asset=norm,
                    path=str(file_path),
                )
            )
            continue
        try:
            raw_bytes = file_path.read_bytes()
            sql_text = raw_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as ex:
            errors.append(
                err(
                    "missing_resource",
                    f"查询文件不可读（须为 UTF-8）: {rel}: {ex}",
                    asset=norm,
                )
            )
            continue
        if not sql_text.strip():
            errors.append(
                err("query_error", f"查询文件为空: {rel}", asset=norm)
            )
            continue
        try:
            root = parse_query(sql_text)
        except QueryShapeError as ex:
            errors.append(
                err("query_error", f"{norm} ({rel}): {ex}", asset=norm)
            )
            continue
        bundle.assets[norm] = Asset(
            name=norm,
            schema_name=target_schema,
            view_name=view_name,
            path=str(file_path),
            rel_path=rel,
            sql_text=sql_text,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            refs=extract_refs(
                root,
                default_schema=target_schema,
                current_database=current_database,
            ),
        )

    if errors:
        return None, errors

    # 源指纹：覆盖清单 schema 与每个视图的（规范名, 文件字节）。预览后任何
    # 文件改动都会改变指纹，apply 据此拒绝过期预览。
    digest = hashlib.sha256()
    digest.update(b"schema\0" + schema.encode("utf-8") + b"\0")
    for name in sorted(bundle.assets):
        asset = bundle.assets[name]
        digest.update(b"view\0" + name.encode("utf-8") + b"\0")
        digest.update(asset.sha256.encode("ascii") + b"\0")
    bundle.digest = "sha256:" + digest.hexdigest()
    return bundle, []
