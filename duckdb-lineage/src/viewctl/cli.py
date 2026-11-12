"""viewctl 命令行入口：把一组视图定义作为一个版本发布到 DuckDB 库文件。

子命令：

  status   查看库内登记状态（版本号、管理集、发布历史）
  preview  预览候选发布：差异计划 + 事务内试装 + 绑定检查，输出应用令牌
  apply    正式应用：核对预览令牌后，在同一事务里完成全部调整与版本登记

退出码：0=ok，2=领域错误（校验/绑定/过期等，详见 errors.py），3=用法或 IO 错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许 `python -m` 与直接脚本两种方式运行；同时兼容 `python -I`
# （隔离模式会忽略 PYTHONPATH 与 user site，这里显式补回）
if __package__ in (None, ""):
    import site

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for _p in site.getusersitepackages(), *site.getsitepackages():
        if _p and _p not in sys.path:
            sys.path.append(_p)

import duckdb  # noqa: E402

from viewctl import core  # noqa: E402


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _error(code: str, message: str) -> dict:
    return {"status": "error", "errors": [{"code": code, "message": message}]}


def _resolve_token(args: argparse.Namespace) -> tuple[tuple[int, str] | None, dict | None]:
    """从 --token-file 或 --expect-* 参数解析预览令牌。"""
    if args.token_file:
        try:
            data = json.loads(Path(args.token_file).read_text(encoding="utf-8"))
            return (int(data["db_version"]), str(data["source_digest"])), None
        except (OSError, ValueError, KeyError, TypeError) as ex:
            return None, _error("io_error", f"令牌文件不可读或格式不正确: {ex}")
    if args.expect_db_version is not None and args.expect_source_digest is not None:
        return (args.expect_db_version, args.expect_source_digest), None
    return None, _error(
        "usage_error",
        "apply 需要预览令牌：--expect-db-version 与 --expect-source-digest，"
        "或 --token-file；请先运行 preview",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="viewctl",
        description="DuckDB 视图组版本化发布工具（预览试装 / 事务应用 / 版本核对）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="查看库内登记状态")
    p_status.add_argument("--db", required=True, help="目标 DuckDB 库文件路径")

    p_preview = sub.add_parser("preview", help="预览候选发布（不改动库）")
    p_preview.add_argument("--db", required=True)
    p_preview.add_argument("--assets", required=True, help="资产目录（含 manifest.json）")

    p_apply = sub.add_parser("apply", help="正式应用候选发布")
    p_apply.add_argument("--db", required=True)
    p_apply.add_argument("--assets", required=True)
    p_apply.add_argument(
        "--expect-db-version",
        type=int,
        default=None,
        help="预览令牌中的库内登记版本",
    )
    p_apply.add_argument(
        "--expect-source-digest",
        default=None,
        help="预览令牌中的源指纹（sha256:...）",
    )
    p_apply.add_argument(
        "--token-file",
        default=None,
        help="预览输出的 token JSON 文件（与 --expect-* 二选一）",
    )

    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        _print(
            _error(
                "io_error",
                f"数据库文件不存在: {db_path}（请先建库并准备好基础表）",
            )
        )
        return 3

    try:
        if args.cmd == "status":
            payload, code = core.status(db_path)
        elif args.cmd == "preview":
            payload, code = core.preview(db_path, args.assets)
        else:  # apply
            token, token_error = _resolve_token(args)
            if token_error is not None:
                _print(token_error)
                return 3
            assert token is not None
            payload, code = core.apply(
                db_path,
                args.assets,
                expect_db_version=token[0],
                expect_source_digest=token[1],
            )
        _print(payload)
        return code
    except (OSError, duckdb.Error) as ex:
        _print(_error("io_error", str(ex)))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
