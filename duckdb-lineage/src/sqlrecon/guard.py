"""查询静态安全护栏：只放行"库内表/视图上的单条只读查询"。

判断不依赖语句开头是不是 SELECT——DML 可以藏在 WITH 的 CTE 里
（``WITH x AS (DELETE ... RETURNING ...) SELECT ...``），写语句也可能带
WITH。这里基于 sqlglot 的完整 AST 做四层检查：

1. 恰好一条语句，根节点只能是 SELECT / UNION / EXCEPT / INTERSECT
   （VALUES 子查询作为派生表可以出现，但顶层裸 VALUES 不接受）；
2. 遍历整棵树，拒绝任何 DML/DDL/事务/连接管理/PRAGMA/SET/CALL/COPY 等
   非查询节点（包括 CTE 体内的写操作与 RETURNING）；
3. 拒绝非确定性表达式：random/rand、uuid、current_date/current_time/
   current_timestamp/localtime/localtimestamp/now/transaction_timestamp 等
   每次运行可能变化的调用；
4. 拒绝读外部文件的表函数与字符串字面量关系：read_csv/read_json/
   parquet_scan/glob/...，以及 ``FROM 'file.parquet'`` 这种写法；
   ATTACH/DETACH 也在第 2 层被拦，外部库无法挂进来。

只读连接（duckdb.connect(..., read_only=True)）是第二道闸门：即使出现
本规则未覆盖的写路径，DuckDB 也会在执行期拒绝。
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .errors import SQLError

DIALECT = "duckdb"

# 根节点允许的查询形态：普通 SELECT 与集合查询（WITH 挂在这些节点上）。
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# 一律拒绝的语句/节点类型（按类名匹配，部分类名在不同 sqlglot 版本可能缺失，
# 用字符串集合 + getattr 兼容）。
_FORBIDDEN_NODE_TYPES = frozenset(
    {
        # 写操作
        "Insert", "Update", "Delete", "Merge", "Copy", "Returning",
        # DDL / 库管理
        "Create", "Alter", "Drop", "Attach", "Detach", "Cache", "Uncache",
        # 会话 / 事务 / 过程化
        "Transaction", "Commit", "Rollback", "Set", "Pragma", "Show",
        "Describe", "Use", "Install", "Command",
    }
)

# 每次运行可能变化、禁止出现的函数名（小写规范化）。
# 只拦截结果依赖"调用时刻/随机源/会话环境"的表达式；strftime(timestamp, fmt)
# 这类纯函数（同样输入必得同样输出）不在拦截范围。
_NONDETERMINISTIC_FUNCS = frozenset(
    {
        "random", "rand", "uuid",
        "now", "current_date", "current_time", "current_timestamp",
        "localtime", "localtimestamp", "transaction_timestamp",
        "get_current_timestamp", "today", "get_current_timezone",
        "epoch_ms",
    }
)

# sqlglot 把一部分当前时间函数解析成专门节点而非 Anonymous。
_NONDETERMINISTIC_NODE_TYPES = frozenset(
    {
        "Rand", "Uuid", "CurrentDate", "CurrentTime", "CurrentTimestamp",
        "Localtime", "Localtimestamp",
    }
)

# 读外部文件/外部资源的表函数：显式名单 + 前缀/后缀模式。
_FILE_TABLE_FUNCS = frozenset(
    {
        "read_csv", "read_csv_auto", "read_json", "read_json_auto",
        "read_parquet", "read_iceberg", "read_delta", "read_blob",
        "read_text", "read_sqlite", "read_postgres", "read_mysql",
        "read_arrow", "read_ipc", "read_excel", "read_xlsx",
        "parquet_scan", "parquet_metadata", "parquet_kv_metadata",
        "parquet_file_metadata", "csv_sqlizer", "glob", "s3", "s4",
        "gs", "azure_blob_storage", "list_archive", "query_table",
        "sqlite_scan", "postgres_scan", "mysql_scan", "iceberg_scan",
        "delta_scan", "excel_scan", "spatial",
    }
)
_FILE_FUNC_PREFIXES = ("read_",)
_FILE_FUNC_SUFFIXES = ("_scan",)

# 库内元数据/内存表函数：明确允许，避免被文件函数规则误伤。
_ALLOWED_TABLE_FUNCS = frozenset(
    {
        "generate_series", "range", "unnest", "replicate_row",
        "duckdb_tables", "duckdb_views", "duckdb_columns",
        "duckdb_databases", "duckdb_schemas", "duckdb_functions",
        "duckdb_settings", "duckdb_constraints", "duckdb_indexes",
        "information_schema",
    }
)


def parse_and_guard(sql_text: str, *, label: str = "query") -> exp.Expression:
    """解析并执行全部静态检查，返回唯一查询的 AST 根节点。

    任何不满足约束的情况抛 :class:`SQLError`，消息里带 ``label`` 便于区分
    新旧查询。
    """
    try:
        statements = [s for s in sqlglot.parse(sql_text, read=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as ex:
        raise SQLError(
            f"{label} SQL 解析失败: {ex}",
            label=label,
            reason="parse_error",
        ) from ex

    if len(statements) != 1:
        raise SQLError(
            f"{label} 必须恰好包含一条语句，实际 {len(statements)} 条；"
            "多语句、脚本与空输入一律拒绝。",
            label=label,
            reason="multiple_statements",
            statement_count=len(statements),
        )

    root = statements[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        raise SQLError(
            f"{label} 必须是单条只读 SELECT/集合查询，"
            f"收到 {type(root).__name__} 语句。",
            label=label,
            reason="non_query_root",
            root_type=type(root).__name__,
        )

    _forbid_nodes(root, label)
    _forbid_nondeterministic(root, label)
    _forbid_external_relations(root, label)
    return root


def _node_type_name(node: exp.Expression) -> str:
    return type(node).__name__


def _forbid_nodes(root: exp.Expression, label: str) -> None:
    for node in root.walk():
        name = _node_type_name(node)
        if name in _FORBIDDEN_NODE_TYPES:
            raise SQLError(
                f"{label} 包含禁止的 {name} 节点：只允许只读查询，"
                "写语句、DDL、事务、ATTACH/DETACH、PRAGMA/SET、CALL、COPY、"
                "SHOW/DESCRIBE 等一律拒绝（含藏在 WITH CTE 体内的写操作）。",
                label=label,
                reason="forbidden_node",
                node_type=name,
            )


def _func_name(node: exp.Func) -> str:
    # Anonymous 节点（now()/read_csv_auto() 等未具名内置）的 sql_name() 只返回
    # 字面量 'ANONYMOUS'，真实函数名在 .name；具名函数（Rand/Uuid/...）用
    # sql_name()。统一小写比较。
    if isinstance(node, exp.Anonymous):
        return getattr(node, "name", "").lower()
    try:
        return node.sql_name().lower()
    except Exception:  # pragma: no cover - 保底
        return getattr(node, "name", "").lower()


def _forbid_nondeterministic(root: exp.Expression, label: str) -> None:
    for node in root.walk():
        type_name = _node_type_name(node)
        if type_name in _NONDETERMINISTIC_NODE_TYPES:
            raise SQLError(
                f"{label} 使用了非确定性表达式 {type_name}：random/uuid/当前时间等"
                "每次运行可能变化的调用不允许出现在对账查询里。",
                label=label,
                reason="nondeterministic",
                func=type_name.lower(),
            )
        if isinstance(node, exp.Func):
            fname = _func_name(node)
            if fname in _NONDETERMINISTIC_FUNCS:
                raise SQLError(
                    f"{label} 使用了非确定性函数 '{fname}'：random/当前时间等"
                    "每次运行可能变化的调用不允许出现在对账查询里。",
                    label=label,
                    reason="nondeterministic",
                    func=fname,
                )


def _is_file_table_func(name: str) -> bool:
    if name in _FILE_TABLE_FUNCS:
        return True
    if name in _ALLOWED_TABLE_FUNCS:
        return False
    if name.startswith(_FILE_FUNC_PREFIXES):
        return True
    if any(name.endswith(suf) for suf in _FILE_FUNC_SUFFIXES):
        return True
    return False


def _forbid_external_relations(root: exp.Expression, label: str) -> None:
    for node in root.walk():
        if not isinstance(node, exp.Table):
            continue
        this = node.this
        # FROM 'some_file.parquet'：Table.this 是带引号的字符串 Identifier
        if isinstance(this, exp.Identifier) and this.quoted:
            raise SQLError(
                f"{label} 通过字符串字面量关系 '{this.name}' 读取外部文件；"
                "对账查询只能引用库内表与视图。",
                label=label,
                reason="external_file_relation",
                target=this.name,
            )
        # FROM read_csv_auto(...) / parquet_scan(...) 等表函数
        if isinstance(this, exp.Anonymous):
            fname = this.name.lower()
            if _is_file_table_func(fname):
                raise SQLError(
                    f"{label} 使用了访问外部资源的表函数 '{fname}'；"
                    "read_*/ *_scan / glob 等文件表函数一律拒绝，"
                    "对账查询只能引用库内表与视图。",
                    label=label,
                    reason="external_table_function",
                    func=fname,
                )
        if isinstance(this, exp.GenerateSeries):
            continue  # 内存序列，允许
        # 普通表/视图（含 CTE 遮蔽名）不在此层拦截，由只读连接兜底写保护。
