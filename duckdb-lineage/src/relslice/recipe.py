"""裁取配方（recipe）的加载与结构校验。

配方是一个 JSON 文件，描述参与裁取的基础表、各表身份列、表间引用关系和
起始身份::

    {
      "tables": [
        {"name": "shop.customers", "identity": ["customer_id"]},
        {"name": "shop.order_items", "identity": ["order_id", "line_no"]}
      ],
      "references": [
        {"name": "items_order",
         "child": "shop.order_items", "child_columns": ["order_id"],
         "parent": "shop.orders"}
      ],
      "seeds": [
        {"table": "shop.order_items", "identity": [1001, 1]}
      ]
    }

规则（本模块只校验 JSON 形状；与源库的对照由 :mod:`relslice.source` 负责）：

* 表名写作 ``schema.table`` 或 ``table``（缺省 schema 为 ``main``）；名字
  按点号切分，恰好一到两段，各段非空；表名在配方内不得重复；
* 身份列 ``identity`` 是非空列名数组，列名不得重复；身份可以由多列组成；
* 引用给出子表、子表列组、父表；子表列组按位置一一对应父表身份列，
  **排列有意义**；引用名唯一；完全相同的引用（子表+列组+父表）不得重复；
* 起点 ``seeds`` 可省略或为空数组；身份值个数须与对应表身份列数一致，
  值只允许 JSON 整数或字符串（不允许 null / 布尔 / 小数 / 数组 / 对象）；
* 所有违规都抛出 :class:`RecipeError` 并带 ``location``（如
  ``references[2].child_columns[1]``），不默默忽略任何一项。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RecipeError

DEFAULT_SCHEMA = "main"


@dataclass(frozen=True)
class TableRef:
    """全限定表名（schema + table），可哈希，作为各索引的键。"""

    schema: str
    table: str

    @property
    def qname(self) -> str:
        return f"{self.schema}.{self.table}"

    def __str__(self) -> str:  # 报告/错误信息里统一用全限定名
        return self.qname


@dataclass(frozen=True)
class TableSpec:
    """一张参与裁取的基础表及其身份列（列名元组，顺序即身份成分顺序）。"""

    ref: TableRef
    identity: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceSpec:
    """一条子表到父表的引用；child_columns 按位置对应父表身份列。"""

    name: str
    child: TableRef
    child_columns: tuple[str, ...]
    parent: TableRef


@dataclass(frozen=True)
class SeedSpec:
    """一个起始身份：某表身份列的一组具体值（int / str）。"""

    table: TableRef
    identity: tuple[Any, ...]


@dataclass
class Recipe:
    """校验通过后的配方。"""

    tables: dict[TableRef, TableSpec]
    references: tuple[ReferenceSpec, ...]
    seeds: tuple[SeedSpec, ...]


def _err(message: str, location: str) -> RecipeError:
    return RecipeError(message, location=location)


def _require_object(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _err(f"{where} 必须是 JSON 对象，实际是 {type(raw).__name__}", where)
    return raw


def _require_nonempty_string(raw: Any, where: str, what: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise _err(f"{where} 必须是非空字符串（{what}）", where)
    return raw


def _parse_table_name(raw: Any, where: str) -> TableRef:
    name = _require_nonempty_string(raw, where, "表名，写作 schema.table 或 table")
    parts = name.split(".")
    if len(parts) > 2 or any(not p for p in parts):
        raise _err(
            f"{where} 表名 '{name}' 不合法：写作 schema.table 或 table，"
            "至多一个点号且各段非空",
            where,
        )
    if len(parts) == 1:
        return TableRef(schema=DEFAULT_SCHEMA, table=parts[0])
    return TableRef(schema=parts[0], table=parts[1])


def _parse_column_list(raw: Any, where: str, what: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise _err(f"{where} 必须是非空列名数组（{what}）", where)
    cols: list[str] = []
    for idx, item in enumerate(raw):
        spot = f"{where}[{idx}]"
        cols.append(_require_nonempty_string(item, spot, "列名"))
    dup = sorted({c for c in cols if cols.count(c) > 1})
    if dup:
        raise _err(f"{where} 存在重复列名 {dup}", where)
    return tuple(cols)


def _parse_seed_identity(raw: Any, where: str) -> tuple[Any, ...]:
    if not isinstance(raw, list) or not raw:
        raise _err(f"{where} 必须是非空数组（身份各成分的值）", where)
    values: list[Any] = []
    for idx, item in enumerate(raw):
        spot = f"{where}[{idx}]"
        # bool 是 int 的子类，必须先行排除；null/小数/数组/对象同样拒绝
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise _err(
                f"{spot} 身份值只允许 JSON 整数或字符串，"
                f"实际是 {type(item).__name__}",
                spot,
            )
        values.append(item)
    return tuple(values)


def load_recipe(path: str | Path) -> Recipe:
    """加载并校验配方文件；任何不合规都抛 :class:`RecipeError`。"""
    recipe_path = Path(path)
    try:
        raw_text = recipe_path.read_text(encoding="utf-8")
    except OSError as ex:
        raise RecipeError(f"无法读取配方文件 {recipe_path}: {ex}") from ex
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as ex:
        raise RecipeError(f"配方不是合法 JSON: {ex}") from ex

    root = _require_object(raw, "配方根对象")

    # ---- tables -----------------------------------------------------------
    raw_tables = root.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise _err("配方必须包含非空数组字段 'tables'", "tables")
    tables: dict[TableRef, TableSpec] = {}
    for idx, item in enumerate(raw_tables):
        where = f"tables[{idx}]"
        obj = _require_object(item, where)
        ref = _parse_table_name(obj.get("name"), f"{where}.name")
        if ref in tables:
            raise _err(f"{where}.name 表 '{ref}' 在配方中重复声明", f"{where}.name")
        identity = _parse_column_list(obj.get("identity"), f"{where}.identity", "身份列")
        tables[ref] = TableSpec(ref=ref, identity=identity)

    # ---- references -------------------------------------------------------
    raw_refs = root.get("references", [])
    if raw_refs is None:
        raw_refs = []
    if not isinstance(raw_refs, list):
        raise _err("'references' 必须是数组（可省略）", "references")
    references: list[ReferenceSpec] = []
    seen_ref_names: set[str] = set()
    seen_ref_shapes: set[tuple[TableRef, tuple[str, ...], TableRef]] = set()
    for idx, item in enumerate(raw_refs):
        where = f"references[{idx}]"
        obj = _require_object(item, where)
        name = _require_nonempty_string(obj.get("name"), f"{where}.name", "引用名")
        if name in seen_ref_names:
            raise _err(f"{where}.name 引用名 '{name}' 重复", f"{where}.name")
        seen_ref_names.add(name)
        child = _parse_table_name(obj.get("child"), f"{where}.child")
        parent = _parse_table_name(obj.get("parent"), f"{where}.parent")
        for role, ref in (("child", child), ("parent", parent)):
            if ref not in tables:
                raise _err(
                    f"{where}.{role} 引用了未在 tables 中声明的表 '{ref}'",
                    f"{where}.{role}",
                )
        child_columns = _parse_column_list(
            obj.get("child_columns"), f"{where}.child_columns", "子表列组"
        )
        shape = (child, child_columns, parent)
        if shape in seen_ref_shapes:
            raise _err(
                f"{where} 与更早的引用完全重复（子表 {child} 列组 "
                f"{list(child_columns)} -> 父表 {parent}），请删除冗余项",
                where,
            )
        seen_ref_shapes.add(shape)
        references.append(
            ReferenceSpec(name=name, child=child, child_columns=child_columns, parent=parent)
        )

    # ---- seeds ------------------------------------------------------------
    raw_seeds = root.get("seeds", [])
    if raw_seeds is None:
        raw_seeds = []
    if not isinstance(raw_seeds, list):
        raise _err("'seeds' 必须是数组（可省略或为空）", "seeds")
    seeds: list[SeedSpec] = []
    for idx, item in enumerate(raw_seeds):
        where = f"seeds[{idx}]"
        obj = _require_object(item, where)
        table = _parse_table_name(obj.get("table"), f"{where}.table")
        if table not in tables:
            raise _err(
                f"{where}.table 引用了未在 tables 中声明的表 '{table}'",
                f"{where}.table",
            )
        identity = _parse_seed_identity(obj.get("identity"), f"{where}.identity")
        want = len(tables[table].identity)
        if len(identity) != want:
            raise _err(
                f"{where}.identity 有 {len(identity)} 个值，但表 '{table}' 的"
                f"身份由 {want} 列组成，个数必须一致",
                f"{where}.identity",
            )
        seeds.append(SeedSpec(table=table, identity=identity))

    return Recipe(tables=tables, references=tuple(references), seeds=tuple(seeds))
