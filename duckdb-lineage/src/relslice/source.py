"""源库只读访问：结构核对、身份合规校验、按身份取行。

所有读取都发生在调用方（:mod:`relslice.engine`）开启的 **同一个读取事务**
里——结构核对、唯一性/空值检查、起点取行、闭包追踪取行看到同一个数据
快照，即"同一次一致的读取范围"。连接本身以 ``read_only=True`` 打开，源
数据只读。

结构核对规则（任一不符抛 :class:`SourceSchemaError`，带 ``location``）：

* 配方里的表必须作为 **基础表** 存在于源库（视图/缺失同样拒绝）；
* 身份列、引用子表列必须真实存在；
* 身份列类型限整数家族或字符串（VARCHAR）；身份列不得含 NULL，且身份
  值在源表中必须唯一（违反时给出示例重复值）；
* 引用两侧列数一致，且对应位置列的 DuckDB 逻辑类型完全相等（不做隐式
  转换）；列的排列有意义——位置即对应关系。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from .errors import DatabaseOpenError, SourceSchemaError
from .recipe import Recipe, TableRef

# 身份列允许的逻辑类型：整数家族 + 字符串
INTEGER_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
}
STRING_TYPES = {"VARCHAR"}
IDENTITY_TYPES = INTEGER_TYPES | STRING_TYPES


def quote_ident(name: str) -> str:
    """标识符加双引号（内嵌引号翻倍），杜绝拼接注入与关键字冲突。"""
    return '"' + name.replace('"', '""') + '"'


def qualified(ref: TableRef) -> str:
    return f"{quote_ident(ref.schema)}.{quote_ident(ref.table)}"


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    type_text: str  # DuckDB 逻辑类型文本，如 INTEGER / VARCHAR / DECIMAL(10,2)


@dataclass
class TableMeta:
    """源表结构：全限定名、按定义顺序的列、身份列类型。"""

    ref: TableRef
    columns: tuple[ColumnMeta, ...]
    identity: tuple[str, ...]  # 来自配方

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def type_of(self, column: str) -> str:
        for c in self.columns:
            if c.name == column:
                return c.type_text
        raise KeyError(column)


@dataclass
class SourceCatalog:
    """核对通过的源库结构视图；同时承担按身份取行的职责。"""

    con: duckdb.DuckDBPyConnection
    tables: dict[TableRef, TableMeta]

    def fetch_row(self, ref: TableRef, identity: tuple) -> dict[str, object] | None:
        """按身份取整行；找不到返回 None。调用方保证身份无 NULL。"""
        meta = self.tables[ref]
        where = " AND ".join(f"{quote_ident(c)} = ?" for c in meta.identity)
        sql = f"SELECT * FROM {qualified(ref)} WHERE {where}"
        cur = self.con.execute(sql, list(identity))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def open_read_only(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """以只读方式打开源库；打不开明确报错。"""
    path = Path(db_path)
    if not path.exists():
        raise DatabaseOpenError(f"源数据库文件不存在: {path}", database=str(path))
    try:
        return duckdb.connect(str(path), read_only=True)
    except duckdb.Error as ex:
        raise DatabaseOpenError(
            f"无法以只读方式打开源数据库 {path}: {ex}",
            database=str(path),
        ) from ex


def _err(message: str, location: str, **details: object) -> SourceSchemaError:
    return SourceSchemaError(message, location=location, **details)


def _describe_table(con: duckdb.DuckDBPyConnection, ref: TableRef) -> tuple[ColumnMeta, ...]:
    """用 LIMIT 0 查询拿到按定义顺序的列名与精确逻辑类型文本。"""
    cur = con.execute(f"SELECT * FROM {qualified(ref)} LIMIT 0")
    return tuple(ColumnMeta(name=d[0], type_text=str(d[1])) for d in cur.description)


def _check_base_table(con: duckdb.DuckDBPyConnection, ref: TableRef, where: str) -> None:
    rows = con.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [ref.schema, ref.table],
    ).fetchall()
    if not rows:
        raise _err(f"{where} 表 '{ref}' 在源库中不存在", where, table=str(ref))
    if rows[0][0] != "BASE TABLE":
        raise _err(
            f"{where} 对象 '{ref}' 不是基础表（table_type={rows[0][0]}），"
            "裁取只针对基础表",
            where,
            table=str(ref),
            table_type=rows[0][0],
        )


def _check_identity_columns(
    con: duckdb.DuckDBPyConnection, meta: TableMeta, where: str
) -> None:
    """身份列：类型限整数/字符串、无 NULL、全表唯一。"""
    for pos, col in enumerate(meta.identity):
        type_text = meta.type_of(col).upper()
        if type_text not in IDENTITY_TYPES:
            raise _err(
                f"{where}.identity[{pos}] 列 '{col}' 类型为 "
                f"{meta.type_of(col)}；身份列类型限整数或字符串",
                f"{where}.identity[{pos}]",
                table=str(meta.ref),
                column=col,
                type=meta.type_of(col),
            )
    null_cond = " OR ".join(f"{quote_ident(c)} IS NULL" for c in meta.identity)
    n_null = con.execute(
        f"SELECT COUNT(*) FROM {qualified(meta.ref)} WHERE {null_cond}"
    ).fetchone()[0]
    if n_null:
        raise _err(
            f"{where}.identity 身份列在源表中存在 {n_null} 行空值；"
            "身份不允许空值",
            f"{where}.identity",
            table=str(meta.ref),
            null_rows=n_null,
        )
    group_cols = ", ".join(quote_ident(c) for c in meta.identity)
    dup_rows = con.execute(
        f"SELECT {group_cols}, COUNT(*) AS n FROM {qualified(meta.ref)} "
        f"GROUP BY {group_cols} HAVING COUNT(*) > 1 LIMIT 3"
    ).fetchall()
    if dup_rows:
        examples = [list(r[:-1]) for r in dup_rows]
        raise _err(
            f"{where}.identity 身份值在源表中不唯一（示例重复值: {examples}）；"
            "身份必须在源表中确实唯一",
            f"{where}.identity",
            table=str(meta.ref),
            duplicate_examples=examples,
        )


def inspect_source(con: duckdb.DuckDBPyConnection, recipe: Recipe) -> SourceCatalog:
    """在同一读取事务内核对配方与源库结构，返回结构视图。"""
    tables: dict[TableRef, TableMeta] = {}
    for idx, (ref, spec) in enumerate(recipe.tables.items()):
        where = f"tables[{idx}]"
        _check_base_table(con, ref, where)
        columns = _describe_table(con, ref)
        known = {c.name for c in columns}
        for pos, col in enumerate(spec.identity):
            if col not in known:
                raise _err(
                    f"{where}.identity[{pos}] 列 '{col}' 在表 '{ref}' 中不存在",
                    f"{where}.identity[{pos}]",
                    table=str(ref),
                    column=col,
                )
        meta = TableMeta(ref=ref, columns=columns, identity=spec.identity)
        _check_identity_columns(con, meta, where)
        tables[ref] = meta

    # 引用与源库对照：子表列存在、两侧列数一致、对应位置类型完全相等
    for idx, ref_spec in enumerate(recipe.references):
        where = f"references[{idx}]"
        child = tables[ref_spec.child]
        parent = tables[ref_spec.parent]
        child_known = {c.name for c in child.columns}
        for pos, col in enumerate(ref_spec.child_columns):
            if col not in child_known:
                raise _err(
                    f"{where}.child_columns[{pos}] 列 '{col}' 在子表 "
                    f"'{ref_spec.child}' 中不存在",
                    f"{where}.child_columns[{pos}]",
                    table=str(ref_spec.child),
                    column=col,
                )
        if len(ref_spec.child_columns) != len(parent.identity):
            raise _err(
                f"{where} 子表列组有 {len(ref_spec.child_columns)} 列，父表 "
                f"'{ref_spec.parent}' 身份有 {len(parent.identity)} 列；"
                "两侧列数必须一致（按位置对应）",
                where,
                reference=ref_spec.name,
            )
        for pos, (col, id_col) in enumerate(
            zip(ref_spec.child_columns, parent.identity)
        ):
            child_type = child.type_of(col).upper()
            parent_type = parent.type_of(id_col).upper()
            if child_type != parent_type:
                raise _err(
                    f"{where} 第 {pos} 对列类型不一致：子表列 "
                    f"'{ref_spec.child}.{col}' 为 {child.type_of(col)}，父表身份列 "
                    f"'{ref_spec.parent}.{id_col}' 为 {parent.type_of(id_col)}；"
                    "对应位置类型必须完全相等",
                    f"{where}.child_columns[{pos}]",
                    reference=ref_spec.name,
                    child_column=col,
                    parent_column=id_col,
                )
    return SourceCatalog(con=con, tables=tables)
