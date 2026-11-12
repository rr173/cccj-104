"""结果对账端到端演示：建库 -> 跑清单 -> 逐项断言预期结论 -> 穷举交叉核对。

演示覆盖（与 demo/recon/checks.json 对应）：

* 等价嵌套查询与 JOIN 改写通过；集合查询换序通过；
* 错误复制/删除一行被指出多重集合差异（带剩余行与重复次数）；
* 重复键 + 容差的贪心陷阱：贪心会失败，但一对一最大匹配完整配上；
* 每列单独都能配上、整行却配不上的反例；
* 空结果、NULL 键、精确大整数/DECIMAL、误差恰好落阈值、NaN/±Inf；
* 列结构不符、重名列、找不到的键/规则列；
* 写操作藏在 WITH 里、外部文件表函数、random、多语句被静态护栏拒绝；
* 单项查询执行报错后，后续检查照常运行；
* 三类运行结论（全部相同 / 存在差异 / 检查错误）对应退出码 0 / 1 / 2。

运行：python3 -I demo/recon/run_demo.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CLI = ROOT / "src" / "sqlrecon" / "cli.py"
ORACLE = ROOT / "tools" / "exhaustive_recon.py"
DB = HERE / "recon.duckdb"
MANIFEST = HERE / "checks.json"

import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

# 清单里每项的预期（status，以及 error 项的错误码前缀）
EXPECTED = {
    "01_hourly_nested_vs_join": "equal",
    "02_set_query_order_swapped": "equal",
    "03_row_duplicated": "difference",
    "04_row_deleted": "difference",
    "05_tolerance_greedy_trap": "equal",
    "06_per_column_matches_whole_row_not": "difference",
    "07_exact_huge_integer_and_decimal": "equal",
    "08_special_floats": "equal",
    "09_special_float_changed": "difference",
    "10_null_key_groups": "equal",
    "11_both_empty": "equal",
    "12_empty_vs_nonempty": "difference",
    "13_tolerance_at_threshold_exact": "equal",
    "14_tolerance_just_over_threshold": "difference",
    "15_relative_tolerance_boundary": "equal",
    "16_no_keys_whole_table_one_group": "equal",
    "17_schema_signature_mismatch": "error",
    "18_duplicate_output_column": "error",
    "19_unknown_tolerance_column": "error",
    "20_unknown_key_column": "error",
    "21_write_hidden_in_cte": "error",
    "22_external_file_table_function": "error",
    "23_nondeterministic_random": "error",
    "24_multiple_statements": "error",
    "25_runtime_query_error_continues": "error",
    "26_last_check_still_runs_after_error": "equal",
}
EXPECTED_ERROR_CODE_PREFIX = {
    "17_schema_signature_mismatch": "schema_mismatch",
    "18_duplicate_output_column": "schema_mismatch",
    "19_unknown_tolerance_column": "schema_mismatch",
    "20_unknown_key_column": "schema_mismatch",
    "21_write_hidden_in_cte": "sql_rejected",
    "22_external_file_table_function": "sql_rejected",
    "23_nondeterministic_random": "sql_rejected",
    "24_multiple_statements": "sql_rejected",
    "25_runtime_query_error_continues": "sql_execution_error",
}


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print(f"  [OK] {message}")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)


def main() -> int:
    print("== 1) 构建演示库 ==")
    proc = run([sys.executable, "-I", str(HERE / "build_db.py")])
    check(proc.returncode == 0, "演示库重建成功（可重复运行）")

    print("\n== 2) 运行完整检查清单（混合 equal/difference/error）==")
    report_path = HERE / "last_report.json"
    proc = run([
        sys.executable, "-I", str(CLI), "run",
        "--db", str(DB), "--manifest", str(MANIFEST),
        "--output", str(report_path),
    ])
    check(proc.returncode == 2, "存在检查错误时整次命令退出码为 2")
    report = json.loads(proc.stdout)
    check(report["summary"] == {
        "checks_total": 26, "equal": 11, "difference": 6, "error": 9,
    }, f"三类计数符合预期: {report['summary']}")

    print("\n== 3) 逐项断言预期状态与错误码 ==")
    by_name = {c["name"]: c for c in report["checks"]}
    for name, want in EXPECTED.items():
        got = by_name[name]["status"]
        check(got == want, f"{name}: {got} == {want}")
        if want == "error":
            code = by_name[name]["error"]["code"]
            check(
                code == EXPECTED_ERROR_CODE_PREFIX[name],
                f"{name}: 错误码 {code}（与数据差异分开报告）",
            )

    print("\n== 4) 关键场景细节 ==")
    # 复制行：右侧多 1 行剩余
    dup = by_name["03_row_duplicated"]["result"]["differences"][0]
    check(dup["right_unmatched"][0]["count"] == 1, "复制行：右侧剩余 1 行重复行")
    # 删除行：左侧多 1 行剩余
    deleted = by_name["04_row_deleted"]["result"]["differences"][0]
    check(deleted["left_unmatched"][0]["count"] == 1, "删除行：左侧剩余 1 行")
    # 贪心陷阱：完整配对 2 对
    trap = by_name["05_tolerance_greedy_trap"]["result"]
    check(trap["matched_pairs"] == 2 and trap["status"] == "equal",
          "贪心陷阱：最大匹配完整配成 2 对（贪心只能配 1 对）")
    # 整行反例：0 配对
    lock = by_name["06_per_column_matches_whole_row_not"]["result"]
    check(lock["matched_pairs"] == 0, "整行反例：没有任何一整行能配上（0 配对）")
    # 执行报错后下一项仍通过
    check(by_name["26_last_check_still_runs_after_error"]["status"] == "equal",
          "单项执行报错不阻断后续检查")

    print("\n== 5) 独立穷举配对交叉核对（不调用线上匹配函数）==")
    proc = run([
        sys.executable, "-I", str(ORACLE),
        "--db", str(DB), "--manifest", str(MANIFEST),
        "--report", str(report_path),
    ])
    check(proc.returncode == 0, "穷举核对全部与生产结论一致")
    check("失败 0 项" in proc.stdout, "穷举器报告 0 失败")

    print("\n== 6) 三类运行结论的退出码 ==")
    # 只含 equal 的临时清单 -> 0
    only_equal = HERE / "_demo_only_equal.json"
    only_equal.write_text(json.dumps({"checks": [
        {"name": "e", "old_sql_file": "queries/80_empty_left.sql",
         "new_sql_file": "queries/80_empty_right.sql"},
    ]}), encoding="utf-8")
    proc = run([sys.executable, "-I", str(CLI), "run",
                "--db", str(DB), "--manifest", str(only_equal)])
    check(proc.returncode == 0, "全部相同 -> 退出码 0")
    only_equal.unlink()

    # 只含 difference（无 error）-> 1
    only_diff = HERE / "_demo_only_diff.json"
    only_diff.write_text(json.dumps({"checks": [
        {"name": "d", "old_sql_file": "queries/80_empty_left.sql",
         "new_sql_file": "queries/81_empty_vs_row.sql"},
    ]}), encoding="utf-8")
    proc = run([sys.executable, "-I", str(CLI), "run",
                "--db", str(DB), "--manifest", str(only_diff)])
    check(proc.returncode == 1, "存在差异且无检查错误 -> 退出码 1")
    only_diff.unlink()

    check(
        json.loads(
            run([sys.executable, "-I", str(CLI), "run",
                 "--db", str(DB), "--manifest", str(MANIFEST)]).stdout
        )["overall_status"] == "error",
        "混合清单 -> overall_status=error，退出码 2",
    )

    print("\n演示完成：规则执行、差异读法、三类结论与退出码全部核对通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
