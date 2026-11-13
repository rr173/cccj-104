"""目标库写出：同事务原生搬运 + 临时文件原子发布，失败不留残骸。

值保真（本模块的核心职责）：

* 源库（只读）与目标临时库（可写）挂在 **同一个会话** 里，入选行在引擎的
  **同一个事务** 内用 ``INSERT INTO target... SELECT ... FROM source...``
  直接搬运——值始终留在 DuckDB 内部，**不经过 Python 对象**，因此逻辑类型
  与原值原样保留：TIMESTAMP_NS 的纳秒、INTERVAL 的月/日/微秒成分、
  DECIMAL 的精确刻度、BLOB 的原始字节，以及列表/结构体里的同样成分；
* 记录选择与数据搬运属于同一次一致读取：搬运读取的是与闭包追踪相同的
  数据快照，事务不结束、快照不变，不会"算完身份再回源库捞可能已变化的
  字段"；
* 目标路径已存在任何内容时拒绝（先由引擎快速预检，发布时用硬链接
  ``os.link`` 的 **不覆盖** 语义兜底，杜绝并发下悄悄覆盖别人的文件）；
* 全部建表与插入先在 ``<target>.tmp-<pid>`` 临时文件里完成、提交并正常
  关闭，再一次性链接到目标名——任何一步失败都只删除临时文件，目标位置
  不会留下可被当成成功结果的库；
* 输出只承诺 **数据与字段结构**：按源表列名/逻辑类型重建空表（无入选
  记录的表同样保留），不迁移视图、索引或约束定义；
* 不同 schema 下的同名表各归各的 schema，绝不混写。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from .errors import TargetExistsError, TargetWriteError
from .source import SOURCE_CATALOG, TableMeta, qualified, quote_ident, quote_literal

if TYPE_CHECKING:  # 仅注解用，避免模块级依赖
    from .closure import Closure

# 目标临时库在会话里的挂载目录名
TARGET_CATALOG = "target"

# 单条 INSERT 携带的最大身份组数：限流参数个数，超大闭包分多段搬运
_CHUNK_IDENTITIES = 4096


class TargetWriter:
    """目标临时库的生命周期：挂载 -> 同事务搬运 -> 原子发布 / 清理。

    事务边界由引擎掌握（引擎在同一个事务里先做结构核对与闭包追踪，再调
    :meth:`move_rows`，最后提交）；本类只管文件与搬运 SQL。
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, target_path: str | Path) -> None:
        self._con = con
        self.target = Path(target_path)
        self.tmp = self.target.with_name(f"{self.target.name}.tmp-{os.getpid()}")

    def attach(self) -> None:
        """把目标临时库以可写方式挂进会话（文件此时创建，内容为空库）。"""
        self.discard()  # 上次崩溃残留的同名临时文件，安全清理
        try:
            self._con.execute(
                f"ATTACH {quote_literal(str(self.tmp))} "
                f"AS {quote_ident(TARGET_CATALOG)}"
            )
        except duckdb.Error as ex:
            raise TargetWriteError(
                f"无法创建目标临时库 {self.tmp}: {ex}", target=str(self.target)
            ) from ex

    def move_rows(self, tables: list[TableMeta], closure: Closure) -> None:
        """在当前事务里建表并把入选行从源库原生搬进目标临时库。

        ``tables`` 需按稳定顺序（引擎按全限定名排序传入）；每张表按身份
        排序分块搬运，保证同一逻辑配方产出的目标库内容确定。没有入选记录
        的表只建结构。
        """
        try:
            for meta in tables:
                self._con.execute(
                    f"CREATE SCHEMA IF NOT EXISTS "
                    f"{quote_ident(TARGET_CATALOG)}.{quote_ident(meta.ref.schema)}"
                )
                cols = ", ".join(
                    f"{quote_ident(c.name)} {c.type_text}" for c in meta.columns
                )
                self._con.execute(
                    f"CREATE TABLE {qualified(meta.ref, TARGET_CATALOG)} ({cols})"
                )
                identities = closure.identities(meta.ref)
                if identities:
                    self._insert_chunked(meta, identities)
        except duckdb.Error as ex:
            raise TargetWriteError(
                f"写入目标临时库失败: {ex}", target=str(self.target)
            ) from ex

    def _insert_chunked(self, meta: TableMeta, identities: list[tuple]) -> None:
        """按身份分块 INSERT ... SELECT；身份值只作 WHERE 参数（整数/字符串，
        经参数绑定无损），业务列整行由 DuckDB 内部直接搬运。"""
        col_list = ", ".join(quote_ident(c) for c in meta.column_names)
        id_cols = ", ".join(quote_ident(c) for c in meta.identity)
        target = qualified(meta.ref, TARGET_CATALOG)
        source = qualified(meta.ref, SOURCE_CATALOG)
        for start in range(0, len(identities), _CHUNK_IDENTITIES):
            chunk = identities[start : start + _CHUNK_IDENTITIES]
            placeholders = ", ".join(
                "(" + ", ".join("?" for _ in meta.identity) + ")" for _ in chunk
            )
            params = [value for identity in chunk for value in identity]
            self._con.execute(
                f"INSERT INTO {target} ({col_list}) "
                f"SELECT {col_list} FROM {source} "
                f"WHERE ({id_cols}) IN (VALUES {placeholders}) "
                f"ORDER BY {id_cols}",
                params,
            )

    def publish(self) -> Path:
        """把已提交并关闭的临时库原子发布到目标名；返回目标路径。"""
        try:
            # 硬链接发布：目标已存在则 FileExistsError，绝不覆盖
            os.link(self.tmp, self.target)
        except FileExistsError as ex:
            raise TargetExistsError(
                f"目标文件已存在，拒绝覆盖: {self.target}", target=str(self.target)
            ) from ex
        finally:
            self.discard()
        return self.target

    def discard(self) -> None:
        """尽力清理临时文件（含 WAL 侧文件）；目标名绝不沾手。"""
        self.tmp.unlink(missing_ok=True)
        Path(str(self.tmp) + ".wal").unlink(missing_ok=True)
