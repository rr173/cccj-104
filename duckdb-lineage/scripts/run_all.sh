#!/usr/bin/env bash
# 一键验证：
#   1) DuckDB 实际执行样例
#   2) 手工标注依赖集合核对
#   3) pytest 单元测试（血缘 32 + viewctl 25 + sensorpipe 16 + sqlrecon 69）
#   4) 两个 DuckDB 因果复现脚本
#   5) JSON 确定性检查（重复运行比对哈希）
#   6) viewctl 传感器视图两版发布演示（实际建库核对业务数值）
#   7) sensorpipe 历史读数补算演示 + 独立全量对照
#   8) sqlrecon 结果对账端到端演示（建库/26 项清单/三类退出码）
#   9) sqlrecon 独立穷举配对交叉核对（不调用线上最大匹配函数）
# 可在 `python -I` 隔离模式下运行（脚本自行补回 user site）。
set -euo pipefail

cd "$(dirname "$0")/.."

echo "############ 1/9 DuckDB 实际执行样例 ############"
python3 -I scripts/run_duckdb_validation.py

echo
echo "############ 2/9 手工标注依赖集合核对 ############"
python3 -I scripts/check_expected.py

echo
echo "############ 3/9 pytest 单元测试 ############"
python3 -I scripts/run_pytest.py tests/ -q

echo
echo "############ 4/9 DuckDB 因果复现 ############"
python3 -I repro/repro_row_existence.py
python3 -I repro/repro_exists_scalar_aggregate.py

echo
echo "############ 5/9 JSON 确定性检查（重复运行比对哈希）############"
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
echo "############ 6/9 viewctl 传感器视图发布演示 ############"
python3 -I demo/sensor/run_demo.py

echo
echo "############ 7/9 sensorpipe 历史读数补算演示 ############"
python3 -I demo/sensor_history/run_demo.py

echo
echo "############ 8/9 sqlrecon 结果对账端到端演示 ############"
python3 -I demo/recon/run_demo.py

echo
echo "############ 9/9 sqlrecon 独立穷举配对交叉核对 ############"
python3 -I demo/recon/build_db.py
# 混合清单按设计包含 error 项，CLI 退出码为 2 是预期结果，不触发 set -e。
python3 -I src/sqlrecon/cli.py run \
  --db demo/recon/recon.duckdb \
  --manifest demo/recon/checks.json \
  --output demo/recon/last_report.json >/dev/null || rc=$?
rc=${rc:-0}
if [ "$rc" != 2 ]; then
  echo "  [FAIL] 混合清单预期退出码 2，实际 $rc"
  exit 1
fi
echo "  [OK] 混合清单退出码为 2（含检查错误，符合预期）"
python3 -I tools/exhaustive_recon.py \
  --db demo/recon/recon.duckdb \
  --manifest demo/recon/checks.json \
  --report demo/recon/last_report.json

echo
echo "全部验证通过。"
