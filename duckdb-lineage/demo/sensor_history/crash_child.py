"""演示专用的崩溃子进程：在事务中途真实杀掉自己。

只用于 demo/tests 的恢复检查；生产 CLI 不加载此文件。

    python3 -I demo/sensor_history/crash_child.py DB CAL_JSONL

调用受测试的 apply_calibrations，并把 _fault_hook 设为 os.kill(os.getpid(), 9)。
钩子位于"校准与明细结论已写入、小时尚未更新"的事务中间点；进程被 SIGKILL
后 DuckDB 由 WAL 自动回滚未提交事务，重开库应看到三层一起保持旧值。
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)
sys.path.insert(0, str(ROOT / "src"))

from sensorpipe.core import apply_calibrations  # noqa: E402


def main() -> int:
    db, cal_file = sys.argv[1], sys.argv[2]
    text = Path(cal_file).read_text(encoding="utf-8")

    def hook(stage: str) -> None:
        os.kill(os.getpid(), 9)

    payload, code = apply_calibrations(db, text, _fault_hook=hook)
    # 正常情况下不可达：钩子一定先杀掉进程
    print(payload, code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
