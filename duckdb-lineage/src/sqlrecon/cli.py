"""命令行入口：对 DuckDB 文件执行命名检查清单。

用法::

    python3 -I src/sqlrecon/cli.py run --db demo/recon/recon.duckdb \
        --manifest demo/recon/checks.json

输出确定性 JSON 报告到 stdout。退出码：

* 0 = 全部检查结果一致；
* 1 = 存在真实数据差异（检查本身都成功执行）；
* 2 = 至少一项检查错误（清单/SQL 限制/列结构/执行失败等；
      可能同时存在数据差异）；
* 3 = 用法或 IO 错误（清单/库文件无法读取等命令级问题）。
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
    from sqlrecon.engine import exit_code_for, run_checklist
    from sqlrecon.errors import ManifestError, ReconError
else:
    from .engine import exit_code_for, run_checklist
    from .errors import ManifestError, ReconError


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    # 我们的编码不用 NaN/Infinity 字面量（特殊浮点是字符串标记），
    # allow_nan 保底打开仅为防御；数值精度由编码层保证。
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqlrecon",
        description="DuckDB SQL 改写结果对账：实际执行新旧查询并比较多重集合结果",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行检查清单")
    p_run.add_argument("--db", required=True, help="DuckDB 库文件路径（只读打开）")
    p_run.add_argument("--manifest", required=True, help="检查清单 JSON 路径")
    p_run.add_argument(
        "--output",
        help="可选：把 JSON 报告同时写入该文件（stdout 仍输出）",
    )

    args = parser.parse_args(argv)

    try:
        report = run_checklist(args.db, args.manifest)
    except ManifestError as ex:
        _print({"overall_status": "error", "error": ex.to_payload()})
        return 3
    except ReconError as ex:
        # 数据库打不开等命令级错误
        _print({"overall_status": "error", "error": ex.to_payload()})
        return 3
    except OSError as ex:
        _print({"overall_status": "error", "error": {
            "code": "io_error", "message": str(ex),
        }})
        return 3

    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.write(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    return exit_code_for(report)


if __name__ == "__main__":
    raise SystemExit(main())
