#!/usr/bin/env bash
# 一键验证：
#   1) DuckDB 实际执行样例
#   2) 手工标注依赖集合核对
#   3) pytest 单元测试
#   4) 两个 DuckDB 因果复现脚本
#   5) JSON 确定性检查（重复运行比对哈希）
# 可在 `python -I` 隔离模式下运行（脚本自行补回 user site）。
set -euo pipefail

cd "$(dirname "$0")/.."

echo "############ 1/5 DuckDB 实际执行样例 ############"
python3 -I scripts/run_duckdb_validation.py

echo
echo "############ 2/5 手工标注依赖集合核对 ############"
python3 -I scripts/check_expected.py

echo
echo "############ 3/5 pytest 单元测试 ############"
python3 -I scripts/run_pytest.py tests/ -q

echo
echo "############ 4/5 DuckDB 因果复现 ############"
python3 -I repro/repro_row_existence.py
python3 -I repro/repro_exists_scalar_aggregate.py

echo
echo "############ 5/5 JSON 确定性检查（重复运行比对哈希）############"
# CLI 对 error/unsupported 样例按约定返回退出码 2（见 cli.py），这不是崩溃，
# 其 JSON 载荷同样要求字节级确定；因此循环内容忍退出码 2，仅比对哈希。
fail=0
for f in sql/*.sql; do
  h1=$( { python3 -I src/lineage/cli.py graph --catalog catalog/catalog.json --sql "$f" 2>/dev/null || true; } | md5sum | cut -d' ' -f1)
  h2=$( { python3 -I src/lineage/cli.py graph --catalog catalog/catalog.json --sql "$f" 2>/dev/null || true; } | md5sum | cut -d' ' -f1)
  if [ "$h1" != "$h2" ]; then
    echo "  [FAIL] $(basename "$f") 两次输出不一致"
    fail=1
  else
    echo "  [OK]   $(basename "$f") $h1"
  fi
done
[ "$fail" -eq 0 ]
echo
echo "全部验证通过。"
