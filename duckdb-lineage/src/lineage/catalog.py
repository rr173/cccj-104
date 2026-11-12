"""表结构目录（catalog）加载与解析。

目录是一个 JSON 文件，描述 catalog.schema.table 三级结构以及每个基础表的列。
血缘分析只信任目录中登记的基础关系；未登记的表名会产生 ``unknown_relation``，
避免对列身份做无根据的猜测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UnknownRelationError


@dataclass(frozen=True)
class TableSchema:
    catalog: str
    schema: str
    table: str
    columns: tuple[str, ...]

    @property
    def fq_name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.table}"

    def column_identity(self, column: str) -> str:
        """基础列的稳定身份：catalog.schema.table.column。"""
        return f"{self.fq_name}.{column}"


@dataclass
class Catalog:
    default_catalog: str
    default_schema: str
    # key: fq_name(catalog.schema.table)
    _tables: dict[str, TableSchema] = field(default_factory=dict)
    # key: 简单表名 -> fq_name（默认 catalog/schema 下的表可直接写短名）
    _short_names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        catalog = cls(
            default_catalog=data["default_catalog"],
            default_schema=data["default_schema"],
        )
        for entry in data["tables"]:
            ts = TableSchema(
                catalog=entry["catalog"],
                schema=entry["schema"],
                table=entry["table"],
                columns=tuple(entry["columns"]),
            )
            catalog._tables[ts.fq_name] = ts
            # 短名仅登记默认 catalog/schema 中的表，避免跨 schema 同名冲突
            if ts.catalog == catalog.default_catalog and ts.schema == catalog.default_schema:
                catalog._short_names[ts.table] = ts.fq_name
        return catalog

    def resolve_table(
        self,
        name: str,
        db: str | None = None,
        catalog: str | None = None,
    ) -> TableSchema:
        """把 sqlglot 解析出的 (catalog, db/schema, table) 三段解析为目录中的表。"""
        parts = [p for p in (catalog, db, name) if p]
        if len(parts) == 3:
            fq = ".".join(parts)
        elif len(parts) == 2:
            fq = f"{self.default_catalog}.{parts[0]}.{parts[1]}"
        elif len(parts) == 1:
            fq = self._short_names.get(
                parts[0],
                f"{self.default_catalog}.{self.default_schema}.{parts[0]}",
            )
        else:  # pragma: no cover - sqlglot 不会给出空表名
            raise UnknownRelationError(".".join(parts))
        table = self._tables.get(fq)
        if table is None:
            raise UnknownRelationError(fq)
        return table

    def get(self, fq_name: str) -> TableSchema:
        return self._tables[fq_name]

    def all_tables(self) -> list[TableSchema]:
        return [self._tables[k] for k in sorted(self._tables)]
