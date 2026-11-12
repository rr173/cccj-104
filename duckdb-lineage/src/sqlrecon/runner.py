"""只读执行：在同一读取事务里跑一项检查的新旧两份查询。

一致性保证：

* 整个命令只开一个 **只读连接**（``duckdb.connect(..., read_only=True)``），
  每项检查的两份查询包在同一个 ``BEGIN ... ROLLBACK`` 读取事务里，新旧 SQL
  看到的是同一个数据快照，不会比到两个状态；
* 静态护栏（:mod:`sqlrecon.guard`）在执行前拦截写语句/多语句/外部文件/
  非确定性表达式；只读连接是第二道闸门；
* 名称、类型、语句限制或执行错误抛 :class:`ReconError` 子类，与真实数据
  差异严格分开，由调用方决定继续下一项。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from .errors import (
    DatabaseOpenError,
    ExecutionError,
    SchemaMismatchError,
    SQLError,
)
from .guard import parse_and_guard


@dataclass(frozen=True)
class ColumnMeta:
    """一个输出列的名称、DuckDB 逻辑类型字符串与逻辑类型 id。"""

    name: str
    type_text: str  # 如 DECIMAL(20,4) / VARCHAR / BIGINT[]
    type_id: str    # 如 decimal / varchar / list


@dataclass
class QueryResult:
    """一份查询的实际执行结果（全量取回到内存，对账按完整多重集合进行）。"""

    columns: tuple[ColumnMeta, ...]
    rows: list[tuple]


def open_read_only(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """以只读方式打开库文件；打不开（文件缺失/不是 DuckDB 库）明确报错。"""
    path = Path(db_path)
    if not path.exists():
        raise DatabaseOpenError(f"数据库文件不存在: {path}", database=str(path))
    try:
        return duckdb.connect(str(path), read_only=True)
    except duckdb.Error as ex:
        raise DatabaseOpenError(
            f"无法以只读方式打开数据库 {path}: {ex}",
            database=str(path),
        ) from ex


def _describe(cur: duckdb.DuckDBPyCursor) -> tuple[ColumnMeta, ...]:
    metas: list[ColumnMeta] = []
    for entry in cur.description:
        name, type_obj = entry[0], entry[1]
        type_text = str(type_obj)
        # DuckDBPyType 带稳定的逻辑类型 id；老接口只给字符串时从文本推断。
        type_id = getattr(type_obj, "id", None) or _infer_type_id(type_text)
        metas.append(ColumnMeta(name=name, type_text=type_text, type_id=type_id))
    return tuple(metas)


def _infer_type_id(type_text: str) -> str:
    head = type_text.split("(", 1)[0].split("[", 1)[0].strip().lower()
    mapping = {
        "decimal": "decimal",
        "numeric": "decimal",
        "double": "double",
        "float": "float",
        "real": "float",
        "varchar": "varchar",
        "text": "varchar",
        "boolean": "boolean",
        "timestamp": "timestamp",
        "timestamptz": "timestamptz",
    }
    return mapping.get(head, head)


def _run_one(con: duckdb.DuckDBPyConnection, sql_text: str, label: str) -> QueryResult:
    parse_and_guard(sql_text, label=label)  # 静态护栏，失败抛 SQLError
    try:
        cur = con.execute(sql_text)
        columns = _describe(cur)
        rows = cur.fetchall()
    except SQLError:
        raise
    except duckdb.Error as ex:
        # 真实执行错误（缺表/缺列/类型错/只读连接拒绝写入等）与数据差异分开
        raise ExecutionError(
            f"{label} 查询执行失败: {ex}",
            label=label,
            duckdb_error=type(ex).__name__,
        ) from ex
    return QueryResult(columns=columns, rows=rows)


def run_pair(
    con: duckdb.DuckDBPyConnection,
    old_sql: str,
    new_sql: str,
) -> tuple[QueryResult, QueryResult]:
    """在同一读取事务内执行新旧查询并返回两份结果。

    事务以 ROLLBACK 结束：只读查询没有任何要提交的改动，ROLLBACK 只负责
    释放快照，保证下一项检查从干净状态开始。
    """
    con.begin()
    try:
        old_result = _run_one(con, old_sql, "旧查询(old)")
        new_result = _run_one(con, new_sql, "新查询(new)")
    finally:
        con.rollback()
    return old_result, new_result


def validate_schema(
    old: QueryResult, new: QueryResult, *, check_name: str
) -> tuple[ColumnMeta, ...]:
    """核对两边列名、列顺序、DuckDB 逻辑类型完全相符；拒绝重名列。

    通过后返回列元组（两边一致）。任何不符都抛 :class:`SchemaMismatchError`，
    绝不偷偷丢列、不重排、不转成字符串再比较。
    """
    old_cols = old.columns
    new_cols = new.columns

    if len(old_cols) != len(new_cols):
        raise SchemaMismatchError(
            f"检查 '{check_name}' 两边输出列数不同: "
            f"旧 {len(old_cols)} 列 / 新 {len(new_cols)} 列",
            check=check_name,
            reason="column_count",
            old_columns=[c.name for c in old_cols],
            new_columns=[c.name for c in new_cols],
        )

    old_names = [c.name for c in old_cols]
    new_names = [c.name for c in new_cols]
    for side, names in (("旧查询", old_names), ("新查询", new_names)):
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise SchemaMismatchError(
                f"检查 '{check_name}' 的{side}输出存在重名列 {dup}；"
                "重名列无法按名字稳定寻址，明确拒绝。",
                check=check_name,
                reason="duplicate_column",
                side=side,
                duplicates=dup,
            )

    diffs: list[dict[str, str]] = []
    for idx, (oc, nc) in enumerate(zip(old_cols, new_cols)):
        if oc.name != nc.name or oc.type_text != nc.type_text:
            diffs.append(
                {
                    "position": str(idx),
                    "old_name": oc.name,
                    "old_type": oc.type_text,
                    "new_name": nc.name,
                    "new_type": nc.type_text,
                }
            )
    if diffs:
        raise SchemaMismatchError(
            f"检查 '{check_name}' 两边列名/顺序/逻辑类型不一致；"
            "列必须同名、同序、同 DuckDB 逻辑类型，不做隐式类型转换。",
            check=check_name,
            reason="column_signature",
            differences=diffs,
        )
    return old_cols


def validate_named_columns(
    columns: tuple[ColumnMeta, ...],
    keys: tuple[str, ...],
    tolerance_cols: tuple[str, ...],
    *,
    check_name: str,
) -> None:
    """确认清单里点名的键列/容差列真实存在；找不到的列明确拒绝。"""
    available = {c.name: c for c in columns}
    missing_keys = [k for k in keys if k not in available]
    missing_tol = [c for c in tolerance_cols if c not in available]
    problems: list[dict[str, object]] = []
    if missing_keys:
        problems.append({"kind": "key", "missing": missing_keys})
    if missing_tol:
        problems.append({"kind": "tolerance", "missing": missing_tol})
    if problems:
        raise SchemaMismatchError(
            f"检查 '{check_name}' 清单引用了输出中不存在的列；"
            "找不到的键列或规则列一律拒绝，不猜测、不忽略。",
            check=check_name,
            reason="unknown_named_column",
            problems=problems,
            available_columns=[c.name for c in columns],
        )

    # 容差只允许数值逻辑类型；整数/DECIMAL 走 Decimal 精确运算，FLOAT/DOUBLE
    # 走浮点运算，但列本身必须是数值列。
    numeric_ids = {
        "tinyint", "smallint", "integer", "bigint", "hugeint",
        "utinyint", "usmallint", "uinteger", "ubigint",
        "decimal", "float", "double",
    }
    bad = [
        {"column": c, "type": available[c].type_text}
        for c in tolerance_cols
        if available[c].type_id not in numeric_ids
    ]
    if bad:
        raise SchemaMismatchError(
            f"检查 '{check_name}' 容差只能配置在数值列上；"
            f"非数值列声明容差被拒绝: {bad}",
            check=check_name,
            reason="tolerance_on_non_numeric",
            columns=bad,
        )
