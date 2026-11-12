"""面向 DuckDB SQL 的列级血缘抽取器（sqlglot 驱动）。"""

from .analyzer import LineageAnalyzer
from .catalog import Catalog
from .errors import (
    AmbiguousColumnError,
    LineageError,
    LineageParseError,
    LineageUnsupported,
    UnknownColumnError,
    UnknownRelationError,
)
from .graph import build_graph, downstream_impact, field_dependencies
from .model import LineageResult

__all__ = [
    "LineageAnalyzer",
    "Catalog",
    "LineageResult",
    "LineageError",
    "LineageUnsupported",
    "LineageParseError",
    "AmbiguousColumnError",
    "UnknownColumnError",
    "UnknownRelationError",
    "build_graph",
    "field_dependencies",
    "downstream_impact",
]
