"""裁取编排：配方 -> 源库核对 -> 闭包追踪 -> 同事务搬运 -> 原子发布 -> 追查说明。

流程与一致性保证：

1. 加载并校验配方（纯 JSON 形状）；
2. 目标路径预检：已存在即拒绝，不做任何读取之外的功；
3. 内存会话把源库以只读、目标临时库以可写挂载进来，在 **同一个事务** 里
   完成结构核对、身份合规校验、起点取行、闭包追踪取行 **以及把入选行搬进
   目标临时库**——记录选择与数据搬运属于同一次一致的读取范围，事务不
   结束、快照不变，不会先算完身份再回源库捞可能已变化的字段；
4. 搬运用 ``INSERT ... SELECT`` 在 DuckDB 内部直接进行，不经过 Python
   对象：逻辑类型与原值（TIMESTAMP_NS 纳秒、INTERVAL 月/日/微秒成分、
   嵌套类型里的同样成分）原样保留；
5. 追踪用规范化顺序（表/引用/起点各自排序），保证调整配方中表、关联或
   起点的排列不改变入选集合与解释路径；
6. 提交并正常关闭后，才把临时库一次性原子发布到目标名；任一前置步骤
   失败都只删除临时文件，目标位置不留任何文件；
7. 产出与目标库逐条对得上的追查说明（见 :func:`build_report`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from .closure import Closure, compute_closure
from .errors import RecipeError, TargetExistsError, TargetWriteError
from .recipe import Recipe, TableRef, load_recipe
from .source import INTEGER_TYPES, SourceCatalog, inspect_source, open_source_session
from .writer import TargetWriter


def _canonical_seeds(recipe: Recipe) -> tuple[tuple[TableRef, tuple], ...]:
    """起点按 (表全限定名, 身份值) 排序，重复起点保留（闭包里再去重）。"""
    return tuple(
        sorted(
            ((s.table, s.identity) for s in recipe.seeds),
            key=lambda item: (item[0].qname, item[1]),
        )
    )


def _canonical_references(recipe: Recipe):
    return tuple(sorted(recipe.references, key=lambda r: r.name))


def _check_seed_value_types(recipe: Recipe, catalog: SourceCatalog) -> None:
    """起点身份值的 JSON 类型须与身份列类型匹配（整数列配整数，字符串列配字符串）。"""
    for s_idx, seed in enumerate(recipe.seeds):
        meta = catalog.tables[seed.table]
        for pos, (value, col) in enumerate(zip(seed.identity, meta.identity)):
            type_text = meta.type_of(col).upper()
            where = f"seeds[{s_idx}].identity[{pos}]"
            if type_text in INTEGER_TYPES and not isinstance(value, int):
                raise RecipeError(
                    f"{where} 身份值 {value!r} 类型不符：列 '{seed.table}.{col}' "
                    f"为 {meta.type_of(col)}，应给 JSON 整数",
                    location=where,
                )
            if type_text not in INTEGER_TYPES and not isinstance(value, str):
                raise RecipeError(
                    f"{where} 身份值 {value!r} 类型不符：列 '{seed.table}.{col}' "
                    f"为 {meta.type_of(col)}，应给 JSON 字符串",
                    location=where,
                )


def _rollback_quietly(con: duckdb.DuckDBPyConnection) -> None:
    """失败路径上尽力回滚；事务已不存在（如提交已失败）时忽略二次错误。"""
    try:
        con.rollback()
    except Exception:
        pass


def _close_quietly(con: duckdb.DuckDBPyConnection) -> None:
    """失败路径上尽力关闭连接；忽略二次错误以免掩盖原始异常。"""
    try:
        con.close()
    except Exception:
        pass


def build_report(
    *,
    source_path: str | Path,
    target_path: str | Path,
    recipe: Recipe,
    catalog: SourceCatalog,
    closure: Closure,
    budget: int | None,
) -> dict[str, Any]:
    """生成追查说明：各表入选身份、起点标记、派生记录的来历链、预算用量。

    说明中的身份集合与写入目标库的内容来自同一份闭包结果，二者必然一致；
    演示脚本会实际打开目标库逐表核对这一点。
    """
    tables_payload: list[dict[str, Any]] = []
    for ref in sorted(recipe.tables, key=lambda r: r.qname):
        entries: list[dict[str, Any]] = []
        for identity in closure.identities(ref):
            origin = closure.origins[(ref, identity)]
            # 空路径即起点本身；派生记录的路径至少有一步
            if not origin.path:
                entries.append(
                    {"identity": list(identity), "origin": {"kind": "seed"}}
                )
            else:
                seed_ref, seed_identity = origin.seed
                entries.append(
                    {
                        "identity": list(identity),
                        "origin": {
                            "kind": "derived",
                            "seed": {
                                "table": seed_ref.qname,
                                "identity": list(seed_identity),
                            },
                            "path": [step.to_dict() for step in origin.path],
                        },
                    }
                )
        tables_payload.append(
            {
                "table": ref.qname,
                "identity_columns": list(recipe.tables[ref].identity),
                "selected_count": len(entries),
                "selected": entries,
            }
        )
    return {
        "status": "ok",
        "source": str(source_path),
        "target": str(target_path),
        "budget": {"limit": budget, "used": closure.total},
        "tables": tables_payload,
    }


def extract(
    source_path: str | Path,
    recipe_path: str | Path,
    target_path: str | Path,
    *,
    budget: int | None = None,
) -> dict[str, Any]:
    """执行一次裁取，返回追查说明；失败抛 :class:`SliceError` 子类。"""
    if budget is not None and budget < 0:
        raise RecipeError("预算必须是非负整数", location="--budget")

    recipe = load_recipe(recipe_path)

    target = Path(target_path)
    if target.exists():
        raise TargetExistsError(
            f"目标文件已存在，拒绝覆盖: {target}", target=str(target)
        )

    con = open_source_session(source_path)
    writer = TargetWriter(con, target)
    try:
        writer.attach()
        con.begin()
        try:
            catalog = inspect_source(con, recipe)
            _check_seed_value_types(recipe, catalog)
            closure = compute_closure(
                tables=recipe.tables.keys(),
                references=_canonical_references(recipe),
                seeds=_canonical_seeds(recipe),
                fetch_row=catalog.fetch_row,
                budget=budget,
            )
            # 写出按全限定名排序，行顺序按身份排序，保证同一逻辑配方产出
            # 的目标库内容确定；搬运与上面的追踪读取同属本事务的快照。
            tables_meta = [
                catalog.tables[ref]
                for ref in sorted(catalog.tables, key=lambda r: r.qname)
            ]
            writer.move_rows(tables_meta, closure)
            try:
                con.commit()
            except duckdb.Error as ex:
                raise TargetWriteError(
                    f"目标临时库提交失败: {ex}", target=str(target)
                ) from ex
        except BaseException:
            _rollback_quietly(con)
            raise
        try:
            con.close()  # 正常关闭：临时库落盘完整，随后才可发布
        except duckdb.Error as ex:
            raise TargetWriteError(
                f"目标临时库关闭失败: {ex}", target=str(target)
            ) from ex
    except BaseException:
        _close_quietly(con)
        writer.discard()
        raise

    writer.publish()

    return build_report(
        source_path=source_path,
        target_path=target,
        recipe=recipe,
        catalog=catalog,
        closure=closure,
        budget=budget,
    )
