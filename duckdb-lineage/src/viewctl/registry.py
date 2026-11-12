"""注册表：记录在目标数据库文件里、由本工具维护的对象与版本。

注册表就放在目标库内部的 ``_viewctl`` schema：

  * ``_viewctl.meta(key, value)``      —— ``version`` 为单调递增的发布版本号；
  * ``_viewctl.objects(name, ...)``    —— 当前由本工具管理的视图及其定义文本；
  * ``_viewctl.history(version, ...)`` —— 每次成功发布的一条审计记录。

设计约束：

  * 管理集合 = 注册表内容。发布时碰巧存在于库里的其它视图/表一律是外部
    对象，绝不收编、绝不因候选清单缺少某名字而删除；
  * 预览是只读的：注册表不存在时按"版本 0、空管理集"对待，预览不会创建
    注册表；注册表只在正式应用的事务里按需创建；
  * 版本号落库（``SELECT value FROM _viewctl.meta WHERE key='version'``
    可直接观察），不依赖任何进程内变量。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
import sqlglot
from sqlglot import exp

from .manifest import REGISTRY_SCHEMA
from .refs import extract_refs

META_TABLE = f"{REGISTRY_SCHEMA}.meta"
OBJECTS_TABLE = f"{REGISTRY_SCHEMA}.objects"
HISTORY_TABLE = f"{REGISTRY_SCHEMA}.history"


@dataclass
class ManagedObject:
    name: str  # 规范化 schema.name
    sql_text: str
    content_hash: str
    installed_version: int


@dataclass
class DbState:
    """目标库当前状态快照（只读采集）。"""

    database: str
    version: int = 0
    managed: dict[str, ManagedObject] = field(default_factory=dict)
    views: dict[str, str] = field(default_factory=dict)  # 全部视图（除注册表 schema）
    tables: set[str] = field(default_factory=set)  # 全部表（除注册表 schema）
    external_refs: dict[str, set[str] | None] = field(default_factory=dict)
    # external_refs：非本工具管理的视图 -> 其引用集合；None 表示定义无法解析


def _norm(schema: str, name: str) -> str:
    return f"{schema}.{name}".lower()


def read_state(con: duckdb.DuckDBPyConnection) -> DbState:
    """读取注册表与库内对象清单（不在此处创建任何对象）。"""
    database = con.execute("SELECT current_database()").fetchone()[0]
    state = DbState(database=database)

    for schema, name in con.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = current_database()"
    ).fetchall():
        if schema.lower() == REGISTRY_SCHEMA:
            continue
        state.tables.add(_norm(schema, name))

    for schema, name, sql in con.execute(
        "SELECT schema_name, view_name, sql FROM duckdb_views() "
        "WHERE database_name = current_database()"
    ).fetchall():
        if schema.lower() == REGISTRY_SCHEMA:
            continue
        state.views[_norm(schema, name)] = sql

    registry_tables = {
        row[0].lower()
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = ?",
            [REGISTRY_SCHEMA],
        ).fetchall()
    }
    if "meta" in registry_tables:
        row = con.execute(
            f"SELECT value FROM {META_TABLE} WHERE key = 'version'"
        ).fetchone()
        if row is not None:
            state.version = int(row[0])
    if "objects" in registry_tables:
        for name, sql_text, content_hash, installed_version in con.execute(
            f"SELECT name, sql_text, content_hash, installed_version "
            f"FROM {OBJECTS_TABLE}"
        ).fetchall():
            state.managed[name] = ManagedObject(
                name=name,
                sql_text=sql_text,
                content_hash=content_hash,
                installed_version=installed_version,
            )

    # 外部视图（非本工具管理）的引用集合：用于撤下保护与影响检查
    for name, sql in sorted(state.views.items()):
        if name in state.managed:
            continue
        default_schema = name.split(".", 1)[0]
        refs: set[str] | None = None
        try:
            statements = [
                s
                for s in sqlglot.parse(sql, read="duckdb")
                if s is not None
            ]
            if len(statements) == 1:
                root = statements[0]
                if isinstance(root, exp.Create):
                    root = root.args.get("expression")
                if isinstance(root, (exp.Select, exp.Union)):
                    refs = extract_refs(
                        root,
                        default_schema=default_schema,
                        current_database=database,
                    )
        except Exception:  # noqa: BLE001 - 外部定义无法解析时保守处理
            refs = None
        state.external_refs[name] = refs
    return state


def ensure_registry(con: duckdb.DuckDBPyConnection) -> None:
    """在事务内按需创建注册表（预览永不调用；只在正式应用时调用）。"""
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{REGISTRY_SCHEMA}"')
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} "
        "(key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"
    )
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {OBJECTS_TABLE} ("
        "name VARCHAR PRIMARY KEY, "
        "schema_name VARCHAR NOT NULL, "
        "view_name VARCHAR NOT NULL, "
        "sql_text VARCHAR NOT NULL, "
        "content_hash VARCHAR NOT NULL, "
        "installed_version INTEGER NOT NULL)"
    )
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} ("
        "version INTEGER NOT NULL, "
        "applied_at TIMESTAMP NOT NULL, "
        "summary VARCHAR NOT NULL)"
    )
    row = con.execute(
        f"SELECT value FROM {META_TABLE} WHERE key = 'version'"
    ).fetchone()
    if row is None:
        con.execute(f"INSERT INTO {META_TABLE} VALUES ('version', '0')")
        con.execute(f"INSERT INTO {META_TABLE} VALUES ('created_by', 'viewctl')")


def read_version_in_txn(con: duckdb.DuckDBPyConnection) -> int:
    """事务内读取当前登记版本（版本核对的权威读取点）。"""
    row = con.execute(
        f"SELECT value FROM {META_TABLE} WHERE key = 'version'"
    ).fetchone()
    return int(row[0]) if row else 0


def record_apply(
    con: duckdb.DuckDBPyConnection,
    *,
    drops: list[str],
    upserts: list[tuple[str, str, str, str, str, int]],
    new_version: int,
    summary_json: str,
) -> None:
    """在同一事务里登记发布结果：撤下删除、变更覆盖、版本号推进。"""
    for name in drops:
        con.execute(f"DELETE FROM {OBJECTS_TABLE} WHERE name = ?", [name])
    for name, schema_name, view_name, sql_text, content_hash, version in upserts:
        con.execute(f"DELETE FROM {OBJECTS_TABLE} WHERE name = ?", [name])
        con.execute(
            f"INSERT INTO {OBJECTS_TABLE} VALUES (?, ?, ?, ?, ?, ?)",
            [name, schema_name, view_name, sql_text, content_hash, version],
        )
    con.execute(
        f"UPDATE {META_TABLE} SET value = ? WHERE key = 'version'",
        [str(new_version)],
    )
    con.execute(
        f"INSERT INTO {HISTORY_TABLE} VALUES (?, current_timestamp, ?)",
        [new_version, summary_json],
    )


def read_history(con: duckdb.DuckDBPyConnection, limit: int = 20) -> list[dict]:
    tables = {
        row[0].lower()
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = ?",
            [REGISTRY_SCHEMA],
        ).fetchall()
    }
    if "history" not in tables:
        return []
    rows = con.execute(
        f"SELECT version, applied_at, summary FROM {HISTORY_TABLE} "
        "ORDER BY version DESC LIMIT ?",
        [limit],
    ).fetchall()
    return [
        {"version": v, "applied_at": str(ts), "summary": summary}
        for v, ts, summary in rows
    ]
