"""命令行入口：按配方从源库裁取关系闭包，产出可独立查询的小库。

用法::

    python3 -I src/relslice/cli.py extract \
        --source demo/relslice/business.duckdb \
        --recipe demo/relslice/recipe.json \
        --target /tmp/extract.duckdb \
        --budget 500 --report /tmp/extract.report.json

输出确定性 JSON 追查说明到 stdout（``--report`` 可同时写文件）。退出码：

* 0 = 裁取成功，目标库与说明已产出；
* 1 = 裁取失败（找不到起始身份 / 选中记录引用断裂 / 预算不足；
      配方与源结构本身合法，是数据内容导致失败）；
* 2 = 命令级错误（配方不合法、配方与源库对不上、源库打不开、
      目标已存在、目标写出失败、IO 错误等）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 兼容 `python -m` 与直接脚本两种方式，且兼容 `python -I` 隔离模式。
if __package__ in (None, ""):
    import site

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for _p in site.getusersitepackages(), *site.getsitepackages():
        if _p and _p not in sys.path:
            sys.path.append(_p)
    from relslice.engine import extract
    from relslice.errors import (
        BudgetExceededError,
        BrokenReferenceError,
        SeedNotFoundError,
        SliceError,
    )
else:
    from .engine import extract
    from .errors import (
        BudgetExceededError,
        BrokenReferenceError,
        SeedNotFoundError,
        SliceError,
    )

# 数据内容导致的裁取失败（退出码 1），与配方/命令错误（退出码 2）分开
_EXTRACTION_FAILURES = (SeedNotFoundError, BrokenReferenceError, BudgetExceededError)


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="relslice",
        description="关系数据裁取：从少量起始记录出发，沿子表->父表引用补入"
        "必需关联资料，产出可独立查询的 DuckDB 小库",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract", help="执行一次裁取")
    p_extract.add_argument("--source", required=True, help="源 DuckDB 库文件（只读打开）")
    p_extract.add_argument("--recipe", required=True, help="裁取配方 JSON 路径")
    p_extract.add_argument("--target", required=True, help="目标 DuckDB 库文件（须不存在）")
    p_extract.add_argument(
        "--budget",
        type=int,
        default=None,
        help="可选：总记录预算（各表入选记录数之和的上限）",
    )
    p_extract.add_argument(
        "--report",
        help="可选：把 JSON 追查说明同时写入该文件（stdout 仍输出）",
    )

    args = parser.parse_args(argv)

    try:
        report = extract(
            args.source, args.recipe, args.target, budget=args.budget
        )
    except _EXTRACTION_FAILURES as ex:
        _print({"status": "failed", "error": ex.to_payload()})
        return 1
    except SliceError as ex:
        _print({"status": "error", "error": ex.to_payload()})
        return 2
    except OSError as ex:
        _print({"status": "error", "error": {
            "code": "io_error", "message": str(ex),
        }})
        return 2

    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.write(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
