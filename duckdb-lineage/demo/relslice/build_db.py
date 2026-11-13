"""构建关系裁取演示库 demo/relslice/business.duckdb（可重复运行）。

库内是手工设计的小数据，每个演示点都能在文档里逐行核对：

* shop.customers / shop.orders / shop.order_items / shop.products —
  客户-订单-明细-商品主链；刻意安排：
  - 订单 1002 与已选订单 1001 同属客户 1（同客户的无关订单，不得被带上）；
  - 明细 (1005,1) 与 (1001,1) 共用商品 P1（共用商品只收一次）；
  - 明细 (1001,3) 的 sku 为 NULL（单列引用为空），(1005,1) 的 price_region
    为 NULL（复合引用部分为空），都不得因此补父记录；
  - 订单 1004 指向不存在的客户 999、明细 (1009,1) 指向不存在的订单
    （未被选中部分的孤儿/断链，不阻止完整裁取；也供"已选路径断裂"演示）；
* shop.price_book — 复合身份 (region, sku)，与 order_items 里
  (sku, price_region) 的自然列序不同，引用必须按父表身份顺序写列；
* shop.item_notes — 经 (line_no, order_id) 引用 order_items 的身份
  (order_id, line_no)，子表列序与父表身份列序相反；
* hr.employees — 员工互相引用：8 <-> 9 互为经理（成环收敛），
  另有 4 <-> 6 一对不连通孤儿环。
"""

from __future__ import annotations

import sys
from pathlib import Path

import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "business.duckdb"

SCHEMA_AND_DATA = [
    "CREATE SCHEMA shop",
    "CREATE SCHEMA hr",
    # --- 客户：2 号是不连通孤儿 ---
    """
    CREATE TABLE shop.customers (
        customer_id INTEGER,
        name        VARCHAR,
        city        VARCHAR
    )
    """,
    """
    INSERT INTO shop.customers VALUES
      (1, 'Ada', '上海'),
      (2, 'Ben', '北京'),
      (3, 'Cid', '深圳')
    """,
    # --- 订单：1002 与 1001 同属客户 1（无关订单）；1003 孤儿；
    #        1004 指向不存在的客户 999（断链，未被主配方选中）---
    """
    CREATE TABLE shop.orders (
        order_id    INTEGER,
        customer_id INTEGER,
        ordered_on  DATE,
        channel     VARCHAR
    )
    """,
    """
    INSERT INTO shop.orders VALUES
      (1001, 1,   DATE '2026-10-01', 'web'),
      (1002, 1,   DATE '2026-10-02', 'web'),
      (1003, 2,   DATE '2026-10-03', 'app'),
      (1004, 999, DATE '2026-10-04', 'web'),
      (1005, 3,   DATE '2026-10-05', 'app')
    """,
    # --- 订单明细：复合身份 (order_id, line_no)；
    #        (1001,3) sku 为空；(1005,1) price_region 为空（复合引用部分为空）；
    #        (1004,1)/(1009,1) 是断链孤儿 ---
    """
    CREATE TABLE shop.order_items (
        order_id     INTEGER,
        line_no      INTEGER,
        sku          VARCHAR,
        price_region VARCHAR,
        qty          INTEGER,
        unit_price   DECIMAL(10,2)
    )
    """,
    """
    INSERT INTO shop.order_items VALUES
      (1001, 1, 'P1', 'north', 2, 10.00),
      (1001, 2, 'P2', 'north', 1, 20.00),
      (1001, 3, NULL, NULL,    5,  1.00),
      (1005, 1, 'P1', NULL,    1, 10.00),
      (1002, 1, 'P3', 'south', 1, 30.00),
      (1004, 1, 'P1', 'north', 1,  5.00),
      (1009, 1, 'P9', 'east',  1,  9.00)
    """,
    # --- 商品：P3/P9 不连通 ---
    """
    CREATE TABLE shop.products (
        sku      VARCHAR,
        title    VARCHAR,
        category VARCHAR
    )
    """,
    """
    INSERT INTO shop.products VALUES
      ('P1', '键盘',   '外设'),
      ('P2', '鼠标',   '外设'),
      ('P3', '显示器', '外设'),
      ('P9', '音箱',   '外设')
    """,
    # --- 价目表：复合身份 (region, sku)，列序与 order_items 的
    #        (sku, price_region) 不同 ---
    """
    CREATE TABLE shop.price_book (
        region     VARCHAR,
        sku        VARCHAR,
        list_price DECIMAL(10,2)
    )
    """,
    """
    INSERT INTO shop.price_book VALUES
      ('north', 'P1', 11.00),
      ('north', 'P2', 21.00),
      ('south', 'P3', 31.00),
      ('east',  'P1', 12.00)
    """,
    # --- 明细备注：经 (line_no, order_id) 引用 order_items (order_id, line_no)，
    #        列序相反；502 指向不存在的明细 (1009,1) ---
    """
    CREATE TABLE shop.item_notes (
        note_id  INTEGER,
        line_no  INTEGER,
        order_id INTEGER,
        body     VARCHAR
    )
    """,
    """
    INSERT INTO shop.item_notes VALUES
      (501, 2, 1001, '礼品包装'),
      (502, 1, 1009, '断链备注')
    """,
    # --- 员工：8 <-> 9 互为经理（成环）；4 <-> 6 是不连通的孤儿环 ---
    """
    CREATE TABLE hr.employees (
        emp_id     INTEGER,
        name       VARCHAR,
        manager_id INTEGER
    )
    """,
    """
    INSERT INTO hr.employees VALUES
      (7, 'G7', 5),
      (5, 'E5', 8),
      (8, 'E8', 9),
      (9, 'E9', 8),
      (3, 'C3', NULL),
      (4, 'D4', 6),
      (6, 'F6', 4)
    """,
]

TABLES = [
    "shop.customers",
    "shop.orders",
    "shop.order_items",
    "shop.products",
    "shop.price_book",
    "shop.item_notes",
    "hr.employees",
]


def build(db_path: Path = DB_PATH) -> Path:
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        for stmt in SCHEMA_AND_DATA:
            con.execute(stmt)
        # 写入后核对行数，避免空表冒充成功
        for table in TABLES:
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  {table:20s} {n} rows")
    finally:
        con.close()
    return db_path


def main() -> int:
    print(f"构建裁取演示库: {DB_PATH}")
    build()
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
