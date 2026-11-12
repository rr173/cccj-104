"""SQL 文本 -> AST 节点的行列定位。

sqlglot 默认不在节点 ``meta`` 中填充位置，而错误诊断（歧义列、unsupported）
需要给审查者精确的行列号。这里在解析时用 ``error_level=RAISE`` 之外保留
token 流，按"节点 SQL 文本在原文中首次匹配的 token 起点"建立定位。
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .errors import ErrorLocation


class PositionMap:
    def __init__(self, sql: str, dialect: str) -> None:
        self.sql = sql
        self.dialect = dialect
        self._tokens = sqlglot.tokenize(sql, read=dialect)
        # 规范化文本 -> token 起点（同文本多处出现时按出现次序消费）
        self._occurrences: dict[str, list[int]] = {}
        for tok in self._tokens:
            self._occurrences.setdefault(tok.text, []).append(tok.start)
        self._cursor: dict[str, int] = {}

    def location_of(self, node: exp.Expression) -> ErrorLocation | None:
        text = self._node_text(node)
        if not text:
            return None
        starts = self._occurrences.get(text)
        if not starts:
            # 复合节点（含空白/关键字变体）退化为其首个列/表名 token
            leaf = self._first_named_leaf(node)
            if leaf is None:
                return None
            return self.location_of(leaf)
        idx = self._cursor.get(text, 0)
        if idx >= len(starts):
            idx = len(starts) - 1
        start = starts[idx]
        self._cursor[text] = idx + 1
        line, col = self._line_col(start)
        return ErrorLocation(line=line, col=col, snippet=node.sql()[:120])

    def _line_col(self, offset: int) -> tuple[int, int]:
        line = self.sql.count("\n", 0, offset) + 1
        last_nl = self.sql.rfind("\n", 0, offset)
        col = offset - last_nl  # 1 起始：换行符之后第一个字符 col=1
        return line, col

    @staticmethod
    def _node_text(node: exp.Expression) -> str:
        if isinstance(node, exp.Column):
            return node.name
        if isinstance(node, exp.Identifier):
            return node.name
        if isinstance(node, exp.Table):
            return node.name
        if isinstance(node, exp.Command) and node.name:
            return node.name
        leaf = PositionMap._first_named_leaf(node)
        return leaf.name if leaf is not None else ""

    @staticmethod
    def _first_named_leaf(node: exp.Expression) -> exp.Expression | None:
        for child in node.walk():
            if isinstance(child, (exp.Column, exp.Identifier, exp.Table)):
                return child
        return None
