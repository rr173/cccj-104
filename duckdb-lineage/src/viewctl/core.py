"""预览 / 应用 / 状态查询的主流程。

预览（preview）：

  1. 只读采集库内状态（注册表版本、管理集、外部视图引用）；
  2. 加载资产清单（静态错误在此报出，不写数据库）；
  3. 计算计划并跑静态防线（循环 / 冲突 / 撤下保护）；
  4. 在事务里 *试装* 候选：逐个执行 DDL，再让 DuckDB 绑定每个最终视图
     （含受影响的存量外部视图），发现列不存在、类型不兼容等问题；
  5. 无论成败一律 ROLLBACK——预览不在库内留下任何变更。

应用（apply）：

  * 先核对源指纹（文件在预览后是否被改动）与库内登记版本（是否有另一
    实例先发布）；任一不符即拒绝，不改动任何内容；
  * 全部视图调整 + 版本登记放进同一事务，任一步失败整体回滚，库回到
    操作前的定义与登记内容；基础表数据本就不在操作范围内。
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from .errors import OpFailure, StaleDatabase, err
from .manifest import Bundle, load_bundle
from .planner import Plan, build_plan
from .registry import (
    DbState,
    ensure_registry,
    read_history,
    read_state,
    read_version_in_txn,
    record_apply,
)

TRIAL_NOTE = "passed (rolled back)"


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _qname(norm_name: str) -> str:
    schema, view = norm_name.split(".", 1)
    return f"{_quote(schema)}.{_quote(view)}"


def _error_payload(errors: list[dict]) -> tuple[dict, int]:
    return {"status": "error", "errors": errors}, 2


# ---------------------------------------------------------------------------
# 执行原语


def _ensure_target_schemas(con: duckdb.DuckDBPyConnection, bundle: Bundle, plan: Plan) -> None:
    targets = sorted(
        {bundle.assets[op.name].schema_name for op in plan.order if op.action != "drop"}
    )
    for schema in targets:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote(schema)}")


def _execute_ops(con: duckdb.DuckDBPyConnection, bundle: Bundle, plan: Plan) -> None:
    for op in plan.order:
        asset = bundle.assets.get(op.name)
        try:
            if op.action == "create":
                # 新建用裸 CREATE：若库内同名对象在预览后被人抢先创建，
                # 这里会失败并回滚，而不是悄悄覆盖别人的对象
                assert asset is not None
                con.execute(f"CREATE VIEW {_qname(op.name)} AS {asset.sql_text}")
            elif op.action == "replace":
                assert asset is not None
                con.execute(
                    f"CREATE OR REPLACE VIEW {_qname(op.name)} AS {asset.sql_text}"
                )
            else:  # drop
                con.execute(f"DROP VIEW IF EXISTS {_qname(op.name)}")
        except duckdb.BinderException as ex:
            raise OpFailure(
                err(
                    "bind_error",
                    f"{op.name} 定义无法通过 DuckDB 绑定: {ex}",
                    asset=op.name,
                    path=asset.path if asset else None,
                )
            ) from ex
        except duckdb.Error as ex:
            raise OpFailure(
                err(
                    "ddl_error",
                    f"执行 {op.action} {op.name} 失败: {ex}",
                    asset=op.name,
                )
            ) from ex


def _bind_check_one(con: duckdb.DuckDBPyConnection, name: str, payload_extra: dict) -> None:
    try:
        con.execute(f"SELECT * FROM {_qname(name)} LIMIT 0")
    except duckdb.Error as ex:
        payload = err("bind_error", f"最终视图 {name} 绑定失败: {ex}", **payload_extra)
        raise OpFailure(payload) from ex


def _bind_check_final(
    con: duckdb.DuckDBPyConnection,
    bundle: Bundle,
    plan: Plan,
    state: DbState,
) -> list[str]:
    """让 DuckDB 绑定每个最终视图；返回实际检查过的视图名（拓扑序）。"""
    checked: list[str] = []
    for name in plan.candidate_order:
        asset = bundle.assets[name]
        _bind_check_one(con, name, {"asset": name, "path": asset.path})
        checked.append(name)
    # 引用了本次变更对象的存量外部视图：它们不可被修改，但也绝不能被
    # 这次发布弄坏——逐一如实绑定验证
    for ext_name, ext_refs in sorted(state.external_refs.items()):
        if ext_refs and ext_refs & plan.changed:
            _bind_check_one(con, ext_name, {"external": ext_name})
            checked.append(ext_name)
    return checked


def _run_trial(
    con: duckdb.DuckDBPyConnection,
    bundle: Bundle,
    plan: Plan,
    state: DbState,
) -> tuple[list[str], list[dict]]:
    """在事务里试装并绑定检查；无论成败都回滚。返回 (已绑定视图, 错误)。"""
    con.execute("BEGIN TRANSACTION")
    try:
        _ensure_target_schemas(con, bundle, plan)
        _execute_ops(con, bundle, plan)
        checked = _bind_check_final(con, bundle, plan, state)
    except OpFailure as ex:
        _rollback_quietly(con)
        return [], [ex.payload]
    except duckdb.Error as ex:
        _rollback_quietly(con)
        return [], [err("ddl_error", f"试装失败: {ex}")]
    _rollback_quietly(con)
    return checked, []


def _rollback_quietly(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("ROLLBACK")
    except duckdb.Error:
        pass


# ---------------------------------------------------------------------------
# 对外流程


def preview(db_path: str | Path, assets_dir: str | Path) -> tuple[dict, int]:
    """预览候选发布：输出计划 + 试装结果 + 应用所需令牌。"""
    con = duckdb.connect(str(db_path))
    try:
        state = read_state(con)
        bundle, errors = load_bundle(assets_dir, current_database=state.database)
        if errors:
            return _error_payload(errors)
        assert bundle is not None
        plan, errors = build_plan(bundle, state)
        if errors:
            return _error_payload(errors)
        assert plan is not None
        checked, errors = _run_trial(con, bundle, plan, state)
        if errors:
            return _error_payload(errors)
        token = {"db_version": state.version, "source_digest": bundle.digest}
        return (
            {
                "status": "ok",
                "database": str(db_path),
                "db_version": state.version,
                "source_digest": bundle.digest,
                "token": token,
                "plan": plan.to_dict(),
                "checks": {
                    "static": "passed",
                    "trial_install": TRIAL_NOTE,
                    "bound_views": checked,
                },
            },
            0,
        )
    finally:
        con.close()


def apply(
    db_path: str | Path,
    assets_dir: str | Path,
    *,
    expect_db_version: int,
    expect_source_digest: str,
) -> tuple[dict, int]:
    """正式应用：核对预览令牌后，把全部变更与版本登记放进同一事务。"""
    con = duckdb.connect(str(db_path))
    try:
        state = read_state(con)
        bundle, errors = load_bundle(assets_dir, current_database=state.database)
        if errors:
            return _error_payload(errors)
        assert bundle is not None

        # 核对一：候选资源在预览后是否被改动（纯文件指纹核对，不写库）
        if bundle.digest != expect_source_digest:
            return _error_payload(
                [
                    err(
                        "stale_source",
                        "资产文件在预览后已变化，请重新 preview 获取新令牌",
                        expected=expect_source_digest,
                        current=bundle.digest,
                    )
                ]
            )

        # 核对二（预检）：库内登记版本是否仍是预览时的版本
        if state.version != expect_db_version:
            return _error_payload(
                [
                    err(
                        "stale_database",
                        "库内登记版本已推进（可能有另一实例先发布了更新），"
                        "请重新 preview",
                        expected=expect_db_version,
                        current=state.version,
                    )
                ]
            )
        plan, errors = build_plan(bundle, state)
        if errors:
            return _error_payload(errors)
        assert plan is not None

        if plan.is_empty():
            # 候选与登记内容完全一致：无需变更，也不推进版本号
            return (
                {
                    "status": "ok",
                    "database": str(db_path),
                    "note": "no_changes",
                    "db_version": state.version,
                    "source_digest": bundle.digest,
                    "plan": plan.to_dict(),
                },
                0,
            )

        con.execute("BEGIN TRANSACTION")
        try:
            ensure_registry(con)
            # 核对二（权威）：事务内再次读取库内版本，与预览令牌比对
            if read_version_in_txn(con) != expect_db_version:
                raise StaleDatabase()
            _ensure_target_schemas(con, bundle, plan)
            _execute_ops(con, bundle, plan)
            checked = _bind_check_final(con, bundle, plan, state)
            new_version = expect_db_version + 1
            record_apply(
                con,
                drops=plan.drops,
                upserts=[
                    (
                        name,
                        bundle.assets[name].schema_name,
                        bundle.assets[name].view_name,
                        bundle.assets[name].sql_text,
                        bundle.assets[name].sha256,
                        new_version,
                    )
                    for name in plan.creates + plan.replaces
                ],
                new_version=new_version,
                summary_json=json.dumps(
                    {
                        "create": plan.creates,
                        "replace": plan.replaces,
                        "drop": plan.drops,
                        "keep": plan.keeps,
                        "source_digest": bundle.digest,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            con.execute("COMMIT")
        except StaleDatabase:
            _rollback_quietly(con)
            return _error_payload(
                [
                    err(
                        "stale_database",
                        "库内登记版本已推进（可能有另一实例先发布了更新），"
                        "请重新 preview",
                        expected=expect_db_version,
                    )
                ]
            )
        except OpFailure as ex:
            _rollback_quietly(con)
            return _error_payload([ex.payload])
        except duckdb.Error as ex:
            _rollback_quietly(con)
            return _error_payload([err("ddl_error", f"发布失败，已整体回滚: {ex}")])

        return (
            {
                "status": "ok",
                "database": str(db_path),
                "applied": [op.to_dict() for op in plan.order],
                "bound_views": checked,
                "db_version": new_version,
                "source_digest": bundle.digest,
            },
            0,
        )
    finally:
        con.close()


def status(db_path: str | Path) -> tuple[dict, int]:
    """查看库内登记状态：版本、管理集、发布历史（全部落库、可观察）。"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        state = read_state(con)
        return (
            {
                "status": "ok",
                "database": str(db_path),
                "db_version": state.version,
                "managed": [
                    {
                        "name": obj.name,
                        "content_hash": obj.content_hash,
                        "installed_version": obj.installed_version,
                    }
                    for obj in (
                        state.managed[name] for name in sorted(state.managed)
                    )
                ],
                "history": read_history(con),
            },
            0,
        )
    finally:
        con.close()
