"""构建结果对账演示库 demo/recon/recon.duckdb（可重复运行）。

库内全部是小表，数据手工设计，每个场景都能在文档里逐行核对：

* sensor_readings   传感器读数，供嵌套查询 vs JOIN 改写、复制/删除行场景
* measurements      重复业务键 k + 数值列，供容差/贪心陷阱/整行配对反例
* exact_numbers     HUGEINT 大整数与高精度 DECIMAL，验证绝不走 float
* edge_numbers      DOUBLE 的 NaN / +Inf / -Inf / NULL 边界
* tiny_groups       NULL 键与空结果场景
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
DB_PATH = HERE / "recon.duckdb"


SCHEMA_AND_DATA = [
    # --- 传感器读数（与 viewctl 演示同构的小数据，便于对照）---
    """
    CREATE TABLE sensor_readings (
        sensor_id   VARCHAR,
        measured_at TIMESTAMP,
        temperature DOUBLE,
        battery     DOUBLE,
        status      VARCHAR
    )
    """,
    """
    INSERT INTO sensor_readings VALUES
      ('s1', TIMESTAMP '2026-03-01 10:05:00', 21.5, 90.0, 'OK'),
      ('s1', TIMESTAMP '2026-03-01 10:35:00', 22.5, 89.0, 'OK'),
      ('s1', TIMESTAMP '2026-03-01 11:05:00', 23.0, 88.0, 'OK'),
      ('s1', TIMESTAMP '2026-03-01 11:50:00', 24.0, 87.0, 'FAULT'),
      ('s2', TIMESTAMP '2026-03-01 10:10:00', 30.0, 60.0, 'OK'),
      ('s2', TIMESTAMP '2026-03-01 10:40:00', 32.0, 59.0, 'OK'),
      ('s2', TIMESTAMP '2026-03-01 11:15:00', 33.0, 58.0, 'OK'),
      ('s2', TIMESTAMP '2026-03-01 11:45:00', 999.0, 57.0, 'OK')
    """,
    # --- 重复键测量值：同键多行，专门构造配对算法场景 ---
    """
    CREATE TABLE measurements (
        grp VARCHAR,
        k   VARCHAR,
        a   DECIMAL(10, 2),
        b   DECIMAL(10, 2),
        d   DOUBLE
    )
    """,
    # 贪心陷阱组 trap：左 [2.04, 1.95]，右（改写值 trap_r）[2.00, 2.10]，
    # abs 容差 0.10。2.04 最近的是 2.00（差 .04），但 1.95 只能配 2.00（差 .05）；
    # 贪心先把 2.00 拿走就会误判不一致，完整配对 2.04->2.10（差 .06）/
    # 1.95->2.00（差 .05）实际可行。
    """
    INSERT INTO measurements VALUES
      ('trap',   'G', 2.04, 0.00, 2.04),
      ('trap',   'G', 1.95, 0.00, 1.95),
      ('trap_r', 'G', 2.00, 0.00, 2.00),
      ('trap_r', 'G', 2.10, 0.00, 2.10)
    """,
    # 整行反例组 rowlock：单列各自都能配上，整行却配不上
    # 左 (1,1)/(2,2)，右 (1,2)/(2,1)
    """
    INSERT INTO measurements VALUES
      ('rowlock', 'H', 1.00, 1.00, 1.0),
      ('rowlock', 'H', 2.00, 2.00, 2.0),
      ('rowlock_r', 'H', 1.00, 2.00, 2.0),
      ('rowlock_r', 'H', 2.00, 1.00, 1.0)
    """,
    # --- 精确数值：HUGEINT 超过 int64，DECIMAL 保留标度 ---
    """
    CREATE TABLE exact_numbers (
        id        INTEGER,
        huge_val  HUGEINT,
        dec_val   DECIMAL(30, 6)
    )
    """,
    """
    INSERT INTO exact_numbers VALUES
      (1, 123456789012345678901234, 123456789012345.123456),
      (2, -987654321098765432109876, -987654321098765.654321),
      (3, 9007199254740993, 0.000001)
    """,
    # --- 浮点边界：NaN / +Inf / -Inf / NULL ---
    """
    CREATE TABLE edge_numbers (
        k VARCHAR,
        f DOUBLE
    )
    """,
    """
    INSERT INTO edge_numbers
      SELECT 'e', CAST('nan'  AS DOUBLE)
    UNION ALL SELECT 'e', CAST('inf'  AS DOUBLE)
    UNION ALL SELECT 'e', CAST('-inf' AS DOUBLE)
    UNION ALL SELECT 'e', 1.5
    UNION ALL SELECT 'e', NULL
    """,
    # --- 小组表：含 NULL 键；另建一张空表演示空结果 ---
    """
    CREATE TABLE tiny_groups (
        k VARCHAR,
        v INTEGER
    )
    """,
    """
    INSERT INTO tiny_groups VALUES
      (NULL, 10),
      (NULL, 10),
      ('a',  1),
      ('a',  2)
    """,
    "CREATE TABLE empty_source (id INTEGER, label VARCHAR)",
]


def build(db_path: Path = DB_PATH) -> Path:
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        for stmt in SCHEMA_AND_DATA:
            con.execute(stmt)
        # 写入后核对行数，避免空表冒充成功
        for table in (
            "sensor_readings", "measurements", "exact_numbers",
            "edge_numbers", "tiny_groups", "empty_source",
        ):
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  {table:16s} {n} rows")
    finally:
        con.close()
    return db_path


def main() -> int:
    print(f"构建对账演示库: {DB_PATH}")
    build()
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
