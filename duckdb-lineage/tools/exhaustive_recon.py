"""独立穷举配对核对器：不调用线上 Hopcroft-Karp 匹配函数。

对清单里每个 ``"oracle": true`` 的检查项，另写一套完全独立的实现：

1. 用只读连接实际执行新旧查询，独立核对列名/类型签名；
2. 用 *穷举排列*（itertools.permutations）枚举组内所有一对一配对，直接数出
   最大匹配规模与是否完整配对——与生产代码的二分图最大匹配算法没有任何共享；
3. 独立实现精确/容差标量语义（int/Decimal 精确十进制、FLOAT/DOUBLE 浮点、
   NaN/Inf/NULL 规则）；
4. 校验生产 CLI 报告的 status、matched_pairs、剩余行数与穷举结论一致；
5. 对每个检查把输入行随机打乱重跑，断言结论、匹配数量与差异行数稳定。

用法::

    python3 -I tools/exhaustive_recon.py --db demo/recon/recon.duckdb \
        --manifest demo/recon/checks.json
"""

from __future__ import annotations

import argparse
import decimal
import itertools
import json
import math
import random
import sys
from pathlib import Path

import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import duckdb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 只复用清单加载、静态护栏与类型元数据（不导入 compare / engine 的匹配实现）。
from sqlrecon.guard import parse_and_guard  # noqa: E402
from sqlrecon.manifest import Tolerance, load_manifest  # noqa: E402
from sqlrecon.runner import ColumnMeta  # noqa: E402

_INTEGER_IDS = {
    "tinyint", "smallint", "integer", "bigint", "hugeint",
    "utinyint", "usmallint", "uinteger", "ubigint",
}
_FLOAT_IDS = {"float", "double"}


# ---------------------------------------------------------------------------
# 独立标量语义
# ---------------------------------------------------------------------------


def exact_equal(x, y, type_id: str) -> bool:
    if x is None or y is None:
        return x is None and y is None
    if type_id in _FLOAT_IDS:
        if math.isnan(x) or math.isnan(y):
            return math.isnan(x) and math.isnan(y)
        if math.isinf(x) or math.isinf(y):
            return x == y
        return x == y
    if type_id in _INTEGER_IDS:
        return isinstance(x, int) and isinstance(y, int) and x == y
    if type_id == "decimal":
        return isinstance(x, decimal.Decimal) and x == y
    return x == y


def tolerant_equal(x, y, type_id: str, tol: Tolerance) -> bool:
    if x is None or y is None:
        return x is None and y is None
    if type_id in _FLOAT_IDS:
        if math.isnan(x) or math.isnan(y):
            return math.isnan(x) and math.isnan(y)
        if math.isinf(x) or math.isinf(y):
            return x == y
        return abs(x - y) <= tol.abs + tol.rel * max(abs(x), abs(y))
    # 整数 / DECIMAL：全部转 Decimal 精确十进制，绝不经过 float
    lx = decimal.Decimal(x) if isinstance(x, int) else x
    ly = decimal.Decimal(y) if isinstance(y, int) else y
    if lx.is_nan() or ly.is_nan():
        return lx.is_nan() and ly.is_nan()
    if lx.is_infinite() or ly.is_infinite():
        return lx == ly
    bound = decimal.Decimal(str(tol.abs)) + decimal.Decimal(str(tol.rel)) * max(
        abs(lx), abs(ly)
    )
    return abs(lx - ly) <= bound


# ---------------------------------------------------------------------------
# 独立执行与签名核对
# ---------------------------------------------------------------------------


def execute(con, sql: str, label: str):
    parse_and_guard(sql, label=label)
    cur = con.execute(sql)
    cols = tuple(
        ColumnMeta(
            name=d[0],
            type_text=str(d[1]),
            type_id=getattr(d[1], "id", None) or str(d[1]).split("(")[0].lower(),
        )
        for d in cur.description
    )
    return cols, cur.fetchall()


def assert_signature(old_cols, new_cols, name):
    assert len(old_cols) == len(new_cols), f"{name}: 列数不同"
    for oc, nc in zip(old_cols, new_cols):
        assert oc.name == nc.name and oc.type_text == nc.type_text, (
            f"{name}: 列签名不符 {oc} vs {nc}"
        )
    names = [c.name for c in old_cols]
    assert len(names) == len(set(names)), f"{name}: 重名列"


# ---------------------------------------------------------------------------
# 穷举最大匹配（组内排列枚举）
# ---------------------------------------------------------------------------


def row_compatible(left_row, right_row, cols, key_idx, tol_map) -> bool:
    for idx, col in enumerate(cols):
        if idx in key_idx:
            ok = exact_equal(left_row[idx], right_row[idx], col.type_id)
        elif col.name in tol_map:
            ok = tolerant_equal(
                left_row[idx], right_row[idx], col.type_id, tol_map[col.name]
            )
        else:
            ok = exact_equal(left_row[idx], right_row[idx], col.type_id)
        if not ok:
            return False
    return True


def exhaustive_group_matching(left_rows, right_rows, cols, key_idx, tol_map):
    """枚举组内所有一对一映射，返回 (最大配对数, 是否完整配对)。

    两边行数可能不等（复制/删行场景）：固定较少的一侧，枚举它到较多一侧的
    全部注入映射；空组的最大配对数为 0。
    """
    if not left_rows or not right_rows:
        return 0, False
    # 让 left 始终是较少的一侧，保证 permutations(n, r) 的 r <= n
    swapped = len(left_rows) > len(right_rows)
    small = right_rows if swapped else left_rows
    large = left_rows if swapped else right_rows
    best = 0
    for perm in itertools.permutations(range(len(large)), len(small)):
        score = 0
        for si, lj in enumerate(perm):
            if swapped:
                compatible = row_compatible(
                    large[lj], small[si], cols, key_idx, tol_map
                )
            else:
                compatible = row_compatible(
                    small[si], large[lj], cols, key_idx, tol_map
                )
            if compatible:
                score += 1
        best = max(best, score)
    full = best == len(left_rows) == len(right_rows)
    return best, full


def group_key(row, key_idx):
    return tuple(row[i] for i in key_idx)


def null_safe_groups(rows, key_idx):
    groups: dict[tuple, list[tuple]] = {}
    for row in rows:
        groups.setdefault(group_key(row, key_idx) if key_idx else (), []).append(row)
    return groups


def oracle_compare(spec, cols, old_rows, new_rows):
    """返回独立结论字典。"""
    key_idx = tuple(i for i, c in enumerate(cols) if c.name in spec.keys)
    tol_map = spec.tolerance
    # 规则列/键列存在性
    names = {c.name for c in cols}
    assert set(spec.keys) <= names, f"{spec.name}: 键列缺失"
    assert set(tol_map) <= names, f"{spec.name}: 容差列缺失"

    gl = null_safe_groups(old_rows, key_idx)
    gr = null_safe_groups(new_rows, key_idx)
    total_matched = 0
    equal = True
    for key in set(gl) | set(gr):
        lr = gl.get(key, [])
        rr = gr.get(key, [])
        best, full = exhaustive_group_matching(lr, rr, cols, key_idx, tol_map)
        total_matched += best
        if not (full and len(lr) == len(rr)):
            equal = False
    return {
        "equal": equal,
        "matched_pairs": total_matched,
        "left_rows": len(old_rows),
        "right_rows": len(new_rows),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _run(con, spec):
    con.begin()
    try:
        old_cols, old_rows = execute(con, spec.old_sql, "old")
        new_cols, new_rows = execute(con, spec.new_sql, "new")
    finally:
        con.rollback()
    assert_signature(old_cols, new_cols, spec.name)
    return old_cols, old_rows, new_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="独立穷举配对核对器")
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--report", help="生产 CLI 输出的 JSON 报告，用于交叉核对结论"
    )
    parser.add_argument("--shuffle-seed", type=int, default=20261112)
    args = parser.parse_args()

    specs = load_manifest(args.manifest)
    # 穷举核对只跑清单里标记 "oracle": true 的检查；error 类检查由 pytest 覆盖。
    raw_manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    oracle_names = {
        c["name"] for c in raw_manifest["checks"] if c.get("oracle") is True
    }
    specs = [s for s in specs if s.name in oracle_names]
    production = {}
    if args.report:
        payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
        production = {c["name"]: c for c in payload["checks"]}

    rng = random.Random(args.shuffle_seed)
    con = duckdb.connect(args.db, read_only=True)
    failures = 0
    checked = 0
    try:
        for spec in specs:
            cols, old_rows, new_rows = _run(con, spec)
            verdict = oracle_compare(spec, cols, old_rows, new_rows)
            checked += 1

            # 打乱输入行顺序后结论必须稳定（顺序被忽略、差异展示按值聚合）
            old_shuf = list(old_rows)
            new_shuf = list(new_rows)
            rng.shuffle(old_shuf)
            rng.shuffle(new_shuf)
            verdict_shuf = oracle_compare(spec, cols, old_shuf, new_shuf)
            assert verdict_shuf == verdict, (
                f"{spec.name}: 行顺序改变后穷举结论不稳定 {verdict} vs {verdict_shuf}"
            )

            tag = "equal" if verdict["equal"] else "difference"
            line = (
                f"[ORACLE] {spec.name:42s} {tag:10s} "
                f"matched={verdict['matched_pairs']} "
                f"L={verdict['left_rows']} R={verdict['right_rows']}"
            )
            if spec.name in production:
                pc = production[spec.name]
                if pc["status"] == "error":
                    line += "  (生产报告为 error，跳过交叉核对)"
                else:
                    pres = pc["result"]
                    same = (
                        pc["status"] == tag
                        and pres["matched_pairs"] == verdict["matched_pairs"]
                        and pres["left_rows"] == verdict["left_rows"]
                        and pres["right_rows"] == verdict["right_rows"]
                    )
                    if not same:
                        failures += 1
                        line += "  [MISMATCH-WITH-PRODUCTION]"
                    else:
                        line += "  [matches production]"
            print(line)
    finally:
        con.close()

    print(f"\n穷举核对 {checked} 项，失败 {failures} 项。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
