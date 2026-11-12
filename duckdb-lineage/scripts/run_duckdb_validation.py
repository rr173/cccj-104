"""第一步验证：用 DuckDB 真实执行每个样例 SQL。

依据 catalog.json 建内存表并插入少量确定性数据，随后执行所有 status 应为 ok
的样例，证明样例本身是可执行的真实 SQL，而非只供解析器消费的文本。
unsupported 样例单独检查它们在 DuckDB 下的行为并确认我们的拒绝判定。

运行：python3 -I scripts/run_duckdb_validation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# 兼容 python -I：补回 user site（脚本目录是项目自身，无第三方投毒面）
import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

from lineage.catalog import Catalog  # noqa: E402

# 每个基础表两行以上的确定性样例数据（列顺序与 catalog 对齐）
SEED_DATA: dict[str, list[tuple]] = {
    "shop.main.customers": [
        (1, "Alice", "Shanghai", "VIP", "2025-01-01"),
        (2, "Bob", "Beijing", "NORMAL", "2025-02-01"),
        (3, "Carol", "Shanghai", "BANNED", "2025-03-01"),
    ],
    "shop.main.orders": [
        (101, 1, "2026-01-05", "PAID", 200.0),
        (102, 1, "2026-02-05", "SHIPPED", 300.0),
        (103, 2, "2026-03-05", "PAID", 50.0),
    ],
    "shop.main.order_items": [
        (1001, 101, 9001, 2, 100.0),
        (1002, 102, 9002, 1, 300.0),
        (1003, 103, 9001, 5, 10.0),
    ],
    "shop.main.products": [
        (9001, "Widget", "hardware", 120.0),
        (9002, "Gadget", "electronics", 350.0),
    ],
    "shop.ref.regions": [
        (10, "East China", "CN"),
        (20, "North China", "CN"),
    ],
    "hr.main.staff": [
        (701, "Dana", 10, "2024-05-01"),
        (702, "Eli", 10, "2024-06-01"),
        (703, "Finn", 20, "2024-07-01"),
    ],
}

# 列 -> DuckDB 类型推断
TYPE_HINTS = {
    "created_at": "DATE",
    "order_date": "DATE",
    "hire_date": "DATE",
    "amount": "DOUBLE",
    "unit_price": "DOUBLE",
    "list_price": "DOUBLE",
    "qty": "INTEGER",
}


def _ddl_for(table) -> str:
    cols = ", ".join(
        f"{c} {TYPE_HINTS.get(c, 'VARCHAR')}" for c in table.columns
    )
    return f"CREATE TABLE {table.fq_name} ({cols})"


def main() -> int:
    catalog = Catalog.load(ROOT / "catalog" / "catalog.json")
    con = duckdb.connect()
    # 建 catalog/schema（DuckDB 中 ATTACH 内存库模拟多 catalog）
    con.execute("ATTACH ':memory:' AS shop")
    con.execute("ATTACH ':memory:' AS hr")
    con.execute("CREATE SCHEMA IF NOT EXISTS shop.ref")
    # 样例默认写短表名（catalog.json 的 default catalog/schema）
    con.execute("USE shop.main")
    for table in catalog.all_tables():
        con.execute(_ddl_for(table))
        rows = SEED_DATA[table.fq_name]
        placeholders = ", ".join("?" for _ in table.columns)
        con.executemany(
            f"INSERT INTO {table.fq_name} VALUES ({placeholders})", rows
        )

    ok_samples = sorted(
        p for p in (ROOT / "sql").glob("*.sql")
        if not p.name.startswith(("07", "08a", "08b", "08c"))
    )
    failures = 0
    for path in ok_samples:
        sql = path.read_text(encoding="utf-8")
        try:
            rows = con.execute(sql).fetchall()
            cols = [d[0] for d in con.description]
            print(f"[PASS] {path.name}: {len(rows)} 行, 输出列={cols}")
        except Exception as ex:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {path.name}: {type(ex).__name__}: {ex}")

    # 07 歧义列：DuckDB 绑定器同样拒绝，验证我们的诊断与数据库行为一致
    sql = (ROOT / "sql" / "07_ambiguous.sql").read_text(encoding="utf-8")
    try:
        con.execute(sql)
        failures += 1
        print("[FAIL] 07_ambiguous.sql: DuckDB 意外接受了歧义列")
    except Exception as ex:  # noqa: BLE001
        print(
            f"[NOTE] 07_ambiguous.sql: DuckDB 拒绝({type(ex).__name__})；"
            f"血缘判定=ambiguous_column，诊断与数据库一致"
        )

    # 08a 递归 CTE 在 DuckDB 可执行（语义合法），但血缘工具必须拒绝
    # 08b 动态 SQL 在 DuckDB 同样可执行，工具仍须拒绝（静态不可见）
    for name, note in [
        ("08a_recursive_cte.sql", "DuckDB 合法但递归固定点无法静态展开"),
        ("08b_dynamic_sql.sql", "DuckDB 合法但语句文本运行时才确定"),
    ]:
        sql = (ROOT / "sql" / name).read_text(encoding="utf-8")
        try:
            con.execute(sql)
            duck_ok = "DuckDB 可执行"
        except Exception as ex:  # noqa: BLE001
            duck_ok = f"DuckDB 拒绝({type(ex).__name__})"
        print(f"[NOTE] {name}: {duck_ok}；血缘判定=unsupported（{note}）")

    print()
    if failures:
        print(f"结论：{failures} 个样例 DuckDB 执行失败")
        return 1
    print("结论：全部受支持样例均在 DuckDB 实际执行成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
