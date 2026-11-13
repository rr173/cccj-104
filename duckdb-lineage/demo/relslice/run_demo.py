"""关系数据裁取端到端演示：建库 -> 裁取 -> 独立集合算法核对 -> 场景矩阵。

核对方式（与"只核对说明条数"严格区分）：

* 用一个 **不调用正式追踪代码** 的小型集合算法（本文件内的
  :func:`oracle_expected`，直接读配方 JSON 与源库、自行做集合闭包）算出
  每张表应选身份；
* 实际打开生成的目标库，逐表核对：身份集合 == 独立算法结果，且入选行的
  **全部字段值** 与源库对应行逐一相等；
* 核对追查说明里的身份集合与目标库实际内容一致（说明与库对得上）。

场景矩阵：成环收敛、多起点重复、复合引用部分为空、共用商品只收一次、
同客户无关订单不带入、不连通孤儿不阻挡、预算刚好够用/少一个名额、
已选路径断裂（含一步与两步链路）、起点身份不存在、空起点输出、
已有目标拒绝、配方排列不变性、源库只读。

运行：python3 -I demo/relslice/run_demo.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CLI = ROOT / "src" / "relslice" / "cli.py"
DB = HERE / "business.duckdb"
RECIPE = HERE / "recipe.json"
WORK = HERE / "_work"

import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

# 主配方的预期入选总数（逐表：客户2 + 订单2 + 明细4 + 备注1 + 商品2 +
# 价目2 + 员工4），预算演示以此为基准
EXPECTED_TOTAL = 17


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print(f"  [OK] {message}")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)


def run_extract(recipe: Path, target: Path, *extra: str) -> tuple[int, dict]:
    proc = run([
        sys.executable, "-I", str(CLI), "extract",
        "--source", str(DB), "--recipe", str(recipe), "--target", str(target),
        *extra,
    ])
    return proc.returncode, json.loads(proc.stdout)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 独立对照：不调用 relslice 任何追踪代码的小型集合算法
# ---------------------------------------------------------------------------


def oracle_expected(recipe_path: Path) -> dict[str, set[tuple]]:
    """直接读配方 JSON 与源库，用集合闭包算出各表应选身份。"""
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    identity_cols = {t["name"]: t["identity"] for t in recipe["tables"]}
    refs = recipe.get("references", [])
    con = duckdb.connect(str(DB), read_only=True)
    try:
        def fetch(table: str, identity: tuple):
            cols = identity_cols[table]
            where = " AND ".join(f'"{c}" = ?' for c in cols)
            cur = con.execute(f"SELECT * FROM {table} WHERE {where}", list(identity))
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([d[0] for d in cur.description], row))

        selected: dict[str, set[tuple]] = {name: set() for name in identity_cols}
        stack: list[tuple[str, tuple]] = []
        for seed in recipe.get("seeds", []):
            table, ident = seed["table"], tuple(seed["identity"])
            if ident in selected[table]:
                continue
            assert fetch(table, ident) is not None, f"oracle: 起点不存在 {seed}"
            selected[table].add(ident)
            stack.append((table, ident))
        while stack:
            table, ident = stack.pop()
            row = fetch(table, ident)
            for ref in refs:
                if ref["child"] != table:
                    continue
                vals = tuple(row[c] for c in ref["child_columns"])
                if any(v is None for v in vals):
                    continue
                if vals in selected[ref["parent"]]:
                    continue
                assert fetch(ref["parent"], vals) is not None, (
                    f"oracle: {table}{ident} 经 {ref['name']} 断链"
                )
                selected[ref["parent"]].add(vals)
                stack.append((ref["parent"], vals))
    finally:
        con.close()
    return selected


def target_identities(db_path: Path, recipe_path: Path) -> dict[str, set[tuple]]:
    """实际打开目标库，逐表读出身份集合。"""
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        out: dict[str, set[tuple]] = {}
        for t in recipe["tables"]:
            cols = t["identity"]
            rows = con.execute(
                f"SELECT {', '.join(chr(34) + c + chr(34) for c in cols)} "
                f"FROM {t['name']}"
            ).fetchall()
            out[t["name"]] = {tuple(r) for r in rows}
        return out
    finally:
        con.close()


def target_rows(db_path: Path, table: str) -> list[tuple]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        con.close()


def source_rows_by_identities(table: str, cols: list[str], identities: set[tuple]) -> list[tuple]:
    con = duckdb.connect(str(DB), read_only=True)
    try:
        rows = []
        for ident in identities:
            where = " AND ".join(f'"{c}" = ?' for c in cols)
            rows.append(con.execute(f"SELECT * FROM {table} WHERE {where}", list(ident)).fetchone())
        return rows
    finally:
        con.close()


def verify_target_against_oracle(target: Path, recipe_path: Path, expected: dict[str, set[tuple]]) -> None:
    """逐表核对：身份集合一致，且入选行全部字段值与源库一致。"""
    actual = target_identities(target, recipe_path)
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    identity_cols = {t["name"]: t["identity"] for t in recipe["tables"]}
    for table in sorted(expected):
        check(
            actual[table] == expected[table],
            f"{table}: 目标库身份集合 == 独立算法结果（{len(expected[table])} 条）",
        )
        got = sorted(target_rows(target, table), key=repr)
        want = sorted(source_rows_by_identities(table, identity_cols[table], expected[table]), key=repr)
        check(got == want, f"{table}: 入选行全部字段值与源库逐一相等")


def report_identities(report: dict) -> dict[str, set[tuple]]:
    return {
        t["table"]: {tuple(s["identity"]) for s in t["selected"]}
        for t in report["tables"]
    }


# ---------------------------------------------------------------------------
# 演示主流程
# ---------------------------------------------------------------------------


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()

    print("== 1) 构建演示库（客户/订单/明细/商品/价目/备注/互相引用的员工）==")
    proc = run([sys.executable, "-I", str(HERE / "build_db.py")])
    check(proc.returncode == 0, "演示库重建成功（可重复运行）")
    source_hash_before = sha256(DB)

    print("\n== 2) 主裁取：7 个起点（含 1 个重复起点）==")
    main_target = WORK / "out_main.duckdb"
    main_report_path = WORK / "report_main.json"
    rc, report = run_extract(RECIPE, main_target, "--report", str(main_report_path))
    check(rc == 0, "主裁取成功，退出码 0")
    check(report["budget"]["used"] == EXPECTED_TOTAL,
          f"实际使用记录预算 == {EXPECTED_TOTAL}")
    check(report["budget"]["limit"] is None, "未设预算时 limit 为 null")

    print("\n== 3) 独立集合算法 + 实际开库逐表核对 ==")
    expected = oracle_expected(RECIPE)
    n_expected = sum(len(v) for v in expected.values())
    check(n_expected == EXPECTED_TOTAL, f"独立算法同样算出 {EXPECTED_TOTAL} 条入选记录")
    verify_target_against_oracle(main_target, RECIPE, expected)
    check(
        report_identities(report) == target_identities(main_target, RECIPE),
        "追查说明中的身份集合与目标库实际内容一致",
    )
    check(
        json.loads(main_report_path.read_text(encoding="utf-8"))["tables"] == report["tables"],
        "--report 文件内容与 stdout 说明一致",
    )

    print("\n== 4) 主裁取的业务语义抽查 ==")
    check(expected["shop.orders"] == {(1001,), (1005,)},
          "订单：只收入 1001/1005，同客户的无关订单 1002 未被带上")
    check(expected["shop.customers"] == {(1,), (3,)},
          "客户：只收入 1/3，孤儿客户 2 不在")
    check(expected["shop.products"] == {("P1",), ("P2",)},
          "商品：共用商品 P1 只收一次，孤儿 P3/P9 不在")
    check(expected["shop.price_book"] == {("north", "P1"), ("north", "P2")},
          "价目表：复合身份按 (region, sku) 正确对应，部分为空的引用未补记录")
    check(expected["hr.employees"] == {(7,), (5,), (8,), (9,)},
          "员工：7->5->8->9 链收敛，8<->9 互引成环各收一次，孤儿环 4/6 不在")
    check(expected["shop.item_notes"] == {(501,)},
          "备注：501 经 (line_no, order_id) 逆序复合引用找到明细 (1001,2)")
    check(expected["shop.order_items"]
          == {(1001, 1), (1001, 2), (1001, 3), (1005, 1)},
          "明细：4 条起点明细入选，断链孤儿 (1004,1)/(1009,1) 不在")

    # 说明里的来历链抽查：客户 1 应从某个明细起点经 items_order+orders_customer 到达
    customers_table = next(t for t in report["tables"] if t["table"] == "shop.customers")
    c1 = next(s for s in customers_table["selected"] if s["identity"] == [1])
    check(
        c1["origin"]["kind"] == "derived"
        and [p["reference"] for p in c1["origin"]["path"]] == ["items_order", "orders_customer"],
        "说明：客户 1 的来历链为 items_order -> orders_customer",
    )
    seed_entries = [
        (t["table"], s["identity"])
        for t in report["tables"] for s in t["selected"]
        if s["origin"]["kind"] == "seed"
    ]
    check(len(seed_entries) == 6, "说明：6 个身份标记为起点（重复起点只算一次）")

    print("\n== 5) 目标库可直接查询（同事视角）==")
    con = duckdb.connect(str(main_target), read_only=True)
    rows = con.execute(
        "SELECT o.order_id, c.name, i.line_no, i.qty "
        "FROM shop.order_items i "
        "JOIN shop.orders o ON o.order_id = i.order_id "
        "JOIN shop.customers c ON c.customer_id = o.customer_id "
        "ORDER BY o.order_id, i.line_no"
    ).fetchall()
    con.close()
    check(len(rows) == 4 and rows[0][0] == 1001,
          f"裁取库内三表联接直接查出 {len(rows)} 行明细-订单-客户")

    print("\n== 6) 预算：刚好够用 / 少一个名额 ==")
    t_fit = WORK / "out_budget_fit.duckdb"
    rc, rep = run_extract(RECIPE, t_fit, "--budget", str(EXPECTED_TOTAL))
    check(rc == 0 and rep["budget"]["used"] == EXPECTED_TOTAL
          and rep["budget"]["limit"] == EXPECTED_TOTAL,
          f"预算 {EXPECTED_TOTAL} 刚好够用：成功且 used==limit")
    t_short = WORK / "out_budget_short.duckdb"
    rc, rep = run_extract(RECIPE, t_short, "--budget", str(EXPECTED_TOTAL - 1))
    check(rc == 1 and rep["error"]["code"] == "budget_exceeded",
          f"预算 {EXPECTED_TOTAL - 1} 少一个名额：退出码 1 + budget_exceeded")
    check(not t_short.exists(), "预算不足时目标位置不留任何文件（无残缺样本）")

    print("\n== 7) 失败场景：断裂 / 起点缺失 ==")
    # 两步链路：明细 (1004,1) -> 订单 1004 -> 客户 999（不存在）
    broken2 = WORK / "recipe_broken2.json"
    broken2.write_text(json.dumps({
        "tables": json.loads(RECIPE.read_text(encoding="utf-8"))["tables"],
        "references": json.loads(RECIPE.read_text(encoding="utf-8"))["references"],
        "seeds": [{"table": "shop.order_items", "identity": [1004, 1]}],
    }), encoding="utf-8")
    rc, rep = run_extract(broken2, WORK / "out_broken2.duckdb")
    err = rep.get("error", {})
    check(rc == 1 and err.get("code") == "broken_reference",
          "已选路径断裂：退出码 1 + broken_reference")
    check(
        err["details"]["reference"] == "orders_customer"
        and err["details"]["missing_parent_identity"] == [999]
        and err["details"]["trail"]["seed"] == {"table": "shop.order_items", "identity": [1004, 1]}
        and [p["reference"] for p in err["details"]["trail"]["path"]] == ["items_order"],
        "断口说明：从起点明细 (1004,1) 经 items_order 到订单 1004，断在客户 999",
    )
    check(not (WORK / "out_broken2.duckdb").exists(), "断裂失败时目标位置不留文件")

    # 一步链路：起点订单 1004 自身的客户引用就是断的（空路径断口）
    broken1 = WORK / "recipe_broken1.json"
    broken1.write_text(json.dumps({
        "tables": json.loads(RECIPE.read_text(encoding="utf-8"))["tables"],
        "references": json.loads(RECIPE.read_text(encoding="utf-8"))["references"],
        "seeds": [{"table": "shop.orders", "identity": [1004]}],
    }), encoding="utf-8")
    rc, rep = run_extract(broken1, WORK / "out_broken1.duckdb")
    err = rep.get("error", {})
    check(
        rc == 1 and err.get("code") == "broken_reference"
        and err["details"]["reference"] == "orders_customer"
        and err["details"]["missing_parent_identity"] == [999]
        and err["details"]["trail"]["path"] == [],
        "空路径断裂：起点订单 1004 自身即断在客户 999",
    )

    # 备注 502 -> 明细 (1009,1) 存在，但再往上订单 1009 不存在
    broken3 = WORK / "recipe_broken3.json"
    broken3.write_text(json.dumps({
        "tables": json.loads(RECIPE.read_text(encoding="utf-8"))["tables"],
        "references": json.loads(RECIPE.read_text(encoding="utf-8"))["references"],
        "seeds": [{"table": "shop.item_notes", "identity": [502]}],
    }), encoding="utf-8")
    rc, rep = run_extract(broken3, WORK / "out_broken3.duckdb")
    err = rep.get("error", {})
    check(
        rc == 1 and err.get("code") == "broken_reference"
        and err["details"]["reference"] == "items_order"
        and err["details"]["missing_parent_identity"] == [1009]
        and [p["reference"] for p in err["details"]["trail"]["path"]] == ["notes_item"],
        "跨两级断裂：备注 502 经 notes_item 到明细 (1009,1)，断在订单 1009",
    )

    # 起点身份不存在
    missing_seed = WORK / "recipe_missing_seed.json"
    missing_seed.write_text(json.dumps({
        "tables": json.loads(RECIPE.read_text(encoding="utf-8"))["tables"],
        "references": json.loads(RECIPE.read_text(encoding="utf-8"))["references"],
        "seeds": [{"table": "shop.orders", "identity": [424242]}],
    }), encoding="utf-8")
    rc, rep = run_extract(missing_seed, WORK / "out_missing_seed.duckdb")
    check(rc == 1 and rep.get("error", {}).get("code") == "seed_not_found"
          and rep["error"]["details"]["identity"] == [424242],
          "起点身份不存在：退出码 1 + seed_not_found（指出表与身份）")

    print("\n== 8) 空起点：全部表保留结构、零记录 ==")
    empty_recipe = WORK / "recipe_empty.json"
    empty_recipe.write_text(json.dumps({
        "tables": json.loads(RECIPE.read_text(encoding="utf-8"))["tables"],
        "references": json.loads(RECIPE.read_text(encoding="utf-8"))["references"],
        "seeds": [],
    }), encoding="utf-8")
    empty_target = WORK / "out_empty.duckdb"
    rc, rep = run_extract(empty_recipe, empty_target)
    check(rc == 0 and rep["budget"]["used"] == 0, "空起点裁取成功，used==0")
    con = duckdb.connect(str(empty_target), read_only=True)
    tables = {r[0] for r in con.execute(
        "SELECT table_schema || '.' || table_name FROM information_schema.tables"
    ).fetchall()}
    n_rows = sum(
        con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in json.loads(RECIPE.read_text(encoding="utf-8"))["tables"]
        for t in [t["name"]]
    )
    item_cols = con.execute("SELECT * FROM shop.order_items LIMIT 0").description
    con.close()
    check(len(tables) == 7, "空起点仍产出全部 7 张表")
    check(n_rows == 0, "空起点各表均为 0 行")
    check(str(item_cols[5][1]) == "DECIMAL(10,2)",
          "空表保留字段类型（unit_price DECIMAL(10,2)）")

    print("\n== 9) 已有目标拒绝覆盖 ==")
    before = sha256(main_target)
    rc, rep = run_extract(RECIPE, main_target)
    check(rc == 2 and rep.get("error", {}).get("code") == "target_exists",
          "目标已存在：退出码 2 + target_exists")
    check(sha256(main_target) == before, "已有目标文件内容未被触碰")

    print("\n== 10) 配方排列不影响入选集合 ==")
    recipe_obj = json.loads(RECIPE.read_text(encoding="utf-8"))
    permuted = dict(recipe_obj)
    permuted["tables"] = list(reversed(recipe_obj["tables"]))
    permuted["references"] = list(reversed(recipe_obj["references"]))
    permuted["seeds"] = list(reversed(recipe_obj["seeds"]))
    perm_path = WORK / "recipe_permuted.json"
    perm_path.write_text(json.dumps(permuted), encoding="utf-8")
    rc, rep_perm = run_extract(perm_path, WORK / "out_permuted.duckdb")
    check(rc == 0 and rep_perm["tables"] == report["tables"],
          "表/关联/起点全部反序后，入选集合与来历说明完全一致")

    print("\n== 11) 源库只读 ==")
    check(sha256(DB) == source_hash_before, "全程源库字节未变（只读打开 + 读取事务）")

    print("\n演示完成：闭包语义、独立算法核对、预算、断裂、空起点、拒绝覆盖全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
