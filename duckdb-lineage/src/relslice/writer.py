"""目标库写出：新 DuckDB 文件，原子发布，失败不留残骸。

保证：

* 目标路径已存在任何内容时拒绝（先由引擎快速预检，发布时用硬链接
  ``os.link`` 的 **不覆盖** 语义兜底，杜绝并发下悄悄覆盖别人的文件）；
* 全部建表与插入先在 ``<target>.tmp-<pid>`` 临时文件里完成并正常关闭，
  再一次性链接到目标名——任何一步失败都只删除临时文件，目标位置不会
  留下可被当成成功结果的库；
* 输出只承诺 **数据与字段结构**：按源表列名/逻辑类型重建空表（无入选
  记录的表同样保留），不迁移视图、索引或约束定义；
* 不同 schema 下的同名表各归各的 schema，绝不混写。
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from .errors import TargetExistsError
from .recipe import TableRef
from .source import TableMeta, qualified, quote_ident


def write_target(
    target_path: str | Path,
    tables: list[TableMeta],
    rows_by_table: dict[TableRef, list[tuple]],
) -> Path:
    """把入选行写入新库文件并原子发布；返回目标路径。

    ``tables`` 需按稳定顺序（引擎按全限定名排序传入）；``rows_by_table``
    给出每张表入选行的值元组（列顺序与表定义一致），缺省或空表只建结构。
    """
    target = Path(target_path)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    if tmp.exists():
        tmp.unlink()  # 上次崩溃残留的同名临时文件，安全清理
    try:
        con = duckdb.connect(str(tmp))
        try:
            for meta in tables:
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(meta.ref.schema)}")
                cols = ", ".join(
                    f"{quote_ident(c.name)} {c.type_text}" for c in meta.columns
                )
                con.execute(f"CREATE TABLE {qualified(meta.ref)} ({cols})")
                rows = rows_by_table.get(meta.ref, [])
                if rows:
                    placeholders = ", ".join("?" for _ in meta.columns)
                    con.executemany(
                        f"INSERT INTO {qualified(meta.ref)} VALUES ({placeholders})",
                        rows,
                    )
        finally:
            con.close()
        try:
            # 硬链接发布：目标已存在则 FileExistsError，绝不覆盖
            os.link(tmp, target)
        except FileExistsError as ex:
            raise TargetExistsError(
                f"目标文件已存在，拒绝覆盖: {target}", target=str(target)
            ) from ex
        finally:
            tmp.unlink(missing_ok=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        # DuckDB 写库可能留下 wal 侧文件，一并清理，避免残骸被误认
        Path(str(tmp) + ".wal").unlink(missing_ok=True)
        raise
    return target
