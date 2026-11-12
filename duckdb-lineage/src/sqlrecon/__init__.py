"""sqlrecon：针对 DuckDB 文件的 SQL 改写结果对账工具。

公开入口：

* :func:`load_manifest` —— 加载命名检查清单；
* :func:`run_checklist` / :func:`exit_code_for` —— 执行对账并得到退出码；
* :class:`ReconError` 及其子类 —— 检查错误（与数据差异区分）。
"""

from __future__ import annotations

from .engine import exit_code_for, run_checklist
from .errors import (
    DatabaseOpenError,
    ExecutionError,
    ManifestError,
    ReconError,
    SchemaMismatchError,
    SQLError,
)
from .manifest import CheckSpec, Tolerance, load_manifest

__all__ = [
    "load_manifest",
    "run_checklist",
    "exit_code_for",
    "CheckSpec",
    "Tolerance",
    "ReconError",
    "ManifestError",
    "SQLError",
    "SchemaMismatchError",
    "ExecutionError",
    "DatabaseOpenError",
]
