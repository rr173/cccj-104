"""缺陷复现 + 修复后断言（可直接运行：python3 -I repro/repro_relslice_value_fidelity.py）。

缺陷：relslice 裁取把行从源库搬进目标库时，先取成 Python 对象再重新插入。
这一进一出两个边界都会丢掉 DuckDB 逻辑类型里的信息：

* ``TIMESTAMP_NS`` -> ``datetime``（只到微秒）-> 再插入：纳秒被截断
  （12:00:00.123456789 变成 12:00:00.123456000）；
* ``INTERVAL '1 month'`` -> ``timedelta(days=30)`` -> 再插入：月份成分被换算成
  固定 30 天，``DATE '2026-02-01' + term`` 在源库是 3 月 1 日，在裁取库里
  变成 3 月 3 日——拿裁取库排查日期条件会查出和原库不同的结果。

脚本分三部分：

A. 边界定位：单独演示"取成 Python 对象再插回"正是值改变的地方（对照组：
   同库 INSERT...SELECT 不经过 Python 对象时值不变）；
B. 裁取复现：按任务给的单行例子（events 表，id=1，纳秒时间戳，1 month
   间隔）实际跑 relslice.extract，重新打开源库与目标库，全部用数据库内的
   精确表达式核对（epoch_ns / datepart 月、日、微秒成分 / 日期加法），
   不把两边取成可能已丢信息的 Python 对象再比较；
C. 修复后断言：同一套核对在修复后必须全部通过。

退出码：0 = 值保真（修复生效）；1 = 复现到值损失（缺陷仍在）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

from relslice.engine import extract  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, message: str) -> None:
    if cond:
        print(f"  [OK]   {message}")
    else:
        FAILURES.append(message)
        print(f"  [FAIL] {message}")


def build_source(path: Path) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE events (id INTEGER, occurred TIMESTAMP_NS, term INTERVAL)"
        )
        con.execute(
            "INSERT INTO events VALUES "
            "(1, TIMESTAMP_NS '2026-02-01 12:00:00.123456789', INTERVAL '1 month')"
        )
    finally:
        con.close()


def write_recipe(path: Path) -> None:
    path.write_text(
        json.dumps({
            "tables": [{"name": "events", "identity": ["id"]}],
            "seeds": [{"table": "events", "identity": [1]}],
        }),
        encoding="utf-8",
    )


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="relslice-fidelity-"))
    src = work / "src.duckdb"
    recipe = work / "recipe.json"
    out = work / "out.duckdb"
    build_source(src)
    write_recipe(recipe)

    print("== A) 边界定位：值在'DuckDB -> Python 对象 -> DuckDB'边界改变 ==")
    con = duckdb.connect(str(src), read_only=True)
    row = con.execute("SELECT occurred, term FROM events WHERE id = 1").fetchone()
    con.close()
    check(
        row[0].microsecond == 123456 and row[1].days == 30,
        "Python 边界取回的对象已丢信息：datetime 只到微秒、间隔只剩 30 days "
        f"（实际取回 {row[0]!r} / {row[1]!r}）",
    )
    roundtrip = work / "roundtrip.duckdb"
    con = duckdb.connect(str(roundtrip))
    con.execute(
        "CREATE TABLE events (id INTEGER, occurred TIMESTAMP_NS, term INTERVAL)"
    )
    con.executemany("INSERT INTO events VALUES (?, ?, ?)", [(1, *row)])
    con.close()
    probe = duckdb.connect()
    probe.execute(f"ATTACH '{src}' AS s (READ_ONLY)")
    probe.execute(f"ATTACH '{roundtrip}' AS r (READ_ONLY)")
    lost_ns, lost_month = probe.execute(
        "SELECT (SELECT occurred FROM s.main.events) <> (SELECT occurred FROM r.main.events), "
        "(SELECT datepart('month', term) FROM s.main.events) <> "
        "(SELECT datepart('month', term) FROM r.main.events)"
    ).fetchone()
    probe.close()
    check(bool(lost_ns), "对照组确认：经 Python 对象回插后纳秒成分改变")
    check(bool(lost_month), "对照组确认：经 Python 对象回插后月份成分改变（1 month -> 30 days）")

    print("\n== B) 裁取复现：relslice.extract 单行例子 ==")
    report = extract(src, recipe, out)
    check(report["status"] == "ok", "裁取返回成功（但业务值是否原样？见下）")

    # 重新打开源库与目标库，挂到同一个核对会话里，全部用数据库内表达式核对
    chk = duckdb.connect()
    chk.execute(f"ATTACH '{src}' AS s (READ_ONLY)")
    chk.execute(f"ATTACH '{out}' AS t (READ_ONLY)")

    print("\n== C) 值保真核对（数据库内精确表达式，不取 Python 对象比较）==")
    ns_src, ns_out = chk.execute(
        "SELECT (SELECT epoch_ns(occurred) FROM s.main.events), "
        "(SELECT epoch_ns(occurred) FROM t.main.events)"
    ).fetchone()
    check(
        ns_src == ns_out == 1769947200123456789,
        f"TIMESTAMP_NS 纳秒原样保留（epoch_ns 源={ns_src} 目标={ns_out}，"
        "期望 1769947200123456789）",
    )
    check(
        chk.execute(
            "SELECT (SELECT occurred FROM s.main.events) = (SELECT occurred FROM t.main.events)"
        ).fetchone()[0]
        is True,
        "数据库内 occurred 全等比较成立",
    )
    comp_src = chk.execute(
        "SELECT datepart('month', term), datepart('day', term), "
        "datepart('microsecond', term) FROM s.main.events"
    ).fetchone()
    comp_out = chk.execute(
        "SELECT datepart('month', term), datepart('day', term), "
        "datepart('microsecond', term) FROM t.main.events"
    ).fetchone()
    check(
        comp_src == comp_out == (1, 0, 0),
        f"INTERVAL 成分原样保留（月/日/微秒 源={comp_src} 目标={comp_out}，期望 (1, 0, 0)）",
    )
    date_src = chk.execute("SELECT DATE '2026-02-01' + term FROM s.main.events").fetchone()[0]
    date_out = chk.execute("SELECT DATE '2026-02-01' + term FROM t.main.events").fetchone()[0]
    check(
        chk.execute(
            "SELECT (SELECT DATE '2026-02-01' + term FROM s.main.events) "
            "= (SELECT DATE '2026-02-01' + term FROM t.main.events) "
            "AND (SELECT DATE '2026-02-01' + term FROM s.main.events) "
            "= TIMESTAMP '2026-03-01 00:00:00'"
        ).fetchone()[0]
        is True,
        f"日期加法结果一致：源库 {date_src}，裁取库 {date_out}（都应是 2026-03-01）",
    )
    types = {
        d[0]: str(d[1])
        for d in chk.execute("SELECT * FROM t.main.events LIMIT 0").description
    }
    check(
        types.get("occurred") == "TIMESTAMP_NS" and types.get("term") == "INTERVAL",
        f"目标库逻辑类型不变（实际 {types}）",
    )
    chk.close()

    if FAILURES:
        print(f"\n复现成立：{len(FAILURES)} 项值保真核对失败（见上 [FAIL]）。")
        return 1
    print("\n全部核对通过：裁取链上值原样保留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
