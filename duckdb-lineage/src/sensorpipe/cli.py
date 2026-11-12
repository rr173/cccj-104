"""sensorpipe 命令行入口。

子命令：

  init                建表（幂等）
  ingest              导入读数 JSONL（--file 或 stdin）
  calibrate           新增/修改/恢复/撤销校准 JSONL（--file 或 stdin）
  detail              查询每条读数的处理结论
  hourly              查询小时指标
  runs                查询处理影响范围
  rebuild             从原始数据全量重算（恢复/对照用）

退出码：0=成功，2=领域错误（校验/冲突/相交等，JSON 里逐条说明），3=IO/用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    import site

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for _p in site.getusersitepackages(), *site.getsitepackages():
        if _p and _p not in sys.path:
            sys.path.append(_p)

import duckdb  # noqa: E402

from sensorpipe import core  # noqa: E402
from sensorpipe.db import init_db  # noqa: E402
from sensorpipe.errors import DomainError, err  # noqa: E402


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _load_text(args: argparse.Namespace) -> str:
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    raise DomainError.one(
        "usage_error",
        "缺少输入：请使用 --file PATH，或加 --stdin 从标准输入读取",
        exit_code=3,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sensorpipe",
        description="传感器历史读数补算链（DuckDB 本地文件）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化库文件（幂等建表）")
    p_init.add_argument("--db", required=True)

    p_ingest = sub.add_parser("ingest", help="导入读数 JSONL")
    p_ingest.add_argument("--db", required=True)
    p_ingest.add_argument("--file", help="JSONL 文件路径")
    p_ingest.add_argument("--stdin", action="store_true", help="从标准输入读取 JSONL")

    p_cal = sub.add_parser("calibrate", help="维护校准记录（新增/修改/恢复/撤销）")
    p_cal.add_argument("--db", required=True)
    p_cal.add_argument("--file", help="JSONL 文件路径")
    p_cal.add_argument("--stdin", action="store_true", help="从标准输入读取 JSONL")

    p_detail = sub.add_parser("detail", help="查询读数处理结论")
    p_detail.add_argument("--db", required=True)
    p_detail.add_argument("--reading-id")
    p_detail.add_argument("--sensor-id")
    p_detail.add_argument("--status", dest="status_filter")
    p_detail.add_argument("--reason")

    p_hourly = sub.add_parser("hourly", help="查询小时指标")
    p_hourly.add_argument("--db", required=True)
    p_hourly.add_argument("--sensor-id")

    p_runs = sub.add_parser("runs", help="查询处理影响范围")
    p_runs.add_argument("--db", required=True)
    p_runs.add_argument("--limit", type=int, default=50)

    p_rebuild = sub.add_parser("rebuild", help="从原始数据全量重算（恢复用）")
    p_rebuild.add_argument("--db", required=True)

    p_status = sub.add_parser("status", help="库内计数与原因分布")
    p_status.add_argument("--db", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if not Path(args.db).is_file() and args.cmd != "init":
            _print({"status": "error", "errors": [
                err("io_error", f"数据库文件不存在: {args.db}（请先运行 init --db）")
            ]})
            return 3
        if args.cmd == "init":
            payload = init_db(args.db)
            _print(payload)
            return 0
        if args.cmd == "ingest":
            text = _load_text(args)
            payload, code = core.import_readings(
                args.db, text, source=args.file or "<stdin>"
            )
            _print(payload)
            return code
        if args.cmd == "calibrate":
            text = _load_text(args)
            payload, code = core.apply_calibrations(
                args.db, text, source=args.file or "<stdin>"
            )
            _print(payload)
            return code
        if args.cmd == "detail":
            payload = core.query_detail(
                args.db,
                reading_id=args.reading_id,
                sensor_id=args.sensor_id,
                status_filter=args.status_filter,
                reason=args.reason,
            )
        elif args.cmd == "hourly":
            payload = core.query_hourly(args.db, sensor_id=args.sensor_id)
        elif args.cmd == "runs":
            payload = core.query_runs(args.db, limit=args.limit)
        elif args.cmd == "rebuild":
            payload, code = core.rebuild_all(args.db)
            _print(payload)
            return code
        else:  # status
            payload, code = core.status(args.db)
            _print(payload)
            return code
        _print(payload)
        return 0
    except DomainError as ex:
        _print({"status": "error", "errors": ex.errors})
        return ex.exit_code
    except (OSError, duckdb.Error) as ex:
        _print({"status": "error", "errors": [err("io_error", str(ex))]})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
