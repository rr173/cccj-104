"""relslice：按引用关系从 DuckDB 业务库裁取最小闭包的小库提取工具。

与血缘抽取器（lineage）等工具相互独立。给定 JSON 配方（基础表、身份列、
子表->父表引用、起始身份），从少量起始记录出发，只沿子表到父表方向补入
必需关联记录，产出可交给同事直接查询的 DuckDB 小库与一份追查说明。

公开入口：

* :func:`load_recipe` —— 加载并校验配方；
* :func:`extract` —— 执行裁取并返回追查说明；
* :func:`compute_closure` —— 纯集合闭包算法（不依赖 duckdb）；
* :class:`SliceError` 及其子类 —— 分层错误（配方/源结构/裁取失败/命令级）。
"""

from __future__ import annotations

from .closure import Closure, Origin, PathStep, compute_closure
from .engine import extract
from .errors import (
    BrokenReferenceError,
    BudgetExceededError,
    DatabaseOpenError,
    RecipeError,
    SeedNotFoundError,
    SliceError,
    SourceSchemaError,
    TargetExistsError,
)
from .recipe import Recipe, ReferenceSpec, SeedSpec, TableRef, TableSpec, load_recipe

__all__ = [
    "extract",
    "load_recipe",
    "compute_closure",
    "Closure",
    "Origin",
    "PathStep",
    "Recipe",
    "ReferenceSpec",
    "SeedSpec",
    "TableRef",
    "TableSpec",
    "SliceError",
    "RecipeError",
    "SourceSchemaError",
    "DatabaseOpenError",
    "TargetExistsError",
    "SeedNotFoundError",
    "BrokenReferenceError",
    "BudgetExceededError",
]
