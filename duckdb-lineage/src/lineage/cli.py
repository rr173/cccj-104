"""命令行入口。

子命令：
  graph       输出确定性 JSON 血缘图
  deps        查询单个输出字段的 value/control 依赖
  impact      查询基础列（或整表/行集）变动的下游影响
  batch       批量分析目录下全部 .sql，输出汇总结果

退出码：0=ok，2=unsupported/error，3=用法或 IO 错误。
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
    from lineage.analyzer import LineageAnalyzer
    from lineage.catalog import Catalog
    from lineage.graph import build_graph, downstream_impact, field_dependencies
else:
    from .analyzer import LineageAnalyzer
    from .catalog import Catalog
    from .graph import build_graph, downstream_impact, field_dependencies


def _load(catalog_path: str, sql_path: str):
    catalog = Catalog.load(catalog_path)
    sql = Path(sql_path).read_text(encoding="utf-8")
    return LineageAnalyzer(catalog), sql, Path(sql_path).name


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lineage",
        description="DuckDB SQL 列级血缘抽取器（sqlglot 解析）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", required=True, help="表结构目录 JSON 路径")
    common.add_argument("--sql", required=True, help="输入 SQL 文件路径")

    p_graph = sub.add_parser("graph", parents=[common], help="输出完整 JSON 血缘图")
    p_graph.add_argument(
        "--raw", action="store_true", help="输出字段级原始结果而非图格式"
    )

    p_deps = sub.add_parser("deps", parents=[common], help="查询字段依赖")
    p_deps.add_argument("--field", required=True, help="输出字段名")

    p_impact = sub.add_parser("impact", parents=[common], help="基础列下游影响查询")
    p_impact.add_argument("--ref", required=True, help="基础列/表/行集身份")

    p_batch = sub.add_parser("batch", help="批量分析 SQL 目录")
    p_batch.add_argument("--catalog", required=True)
    p_batch.add_argument("--sql-dir", required=True)

    args = parser.parse_args(argv)

    try:
        if args.cmd == "batch":
            return _batch(args)
        analyzer, sql, sql_file = _load(args.catalog, args.sql)
        result = analyzer.analyze(sql, sql_file=sql_file)

        if args.cmd == "graph":
            payload = result.to_dict() if args.raw else build_graph(result)
        elif args.cmd == "deps":
            payload = field_dependencies(result, args.field)
        else:  # impact
            payload = downstream_impact(result, args.ref)

        _print(payload)
        return 0 if result.status == "ok" else 2
    except (OSError, KeyError, ValueError) as ex:
        _print({"status": "io_error", "message": str(ex)})
        return 3


def _batch(args: argparse.Namespace) -> int:
    catalog = Catalog.load(args.catalog)
    analyzer = LineageAnalyzer(catalog)
    summary = []
    worst = 0
    for sql_path in sorted(Path(args.sql_dir).glob("*.sql")):
        sql = sql_path.read_text(encoding="utf-8")
        result = analyzer.analyze(sql, sql_file=sql_path.name)
        summary.append(
            {
                "sql_file": sql_path.name,
                "status": result.status,
                "field_count": len(result.fields),
                "errors": result.errors,
            }
        )
        if result.status != "ok":
            worst = 2
    _print({"results": summary})
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
