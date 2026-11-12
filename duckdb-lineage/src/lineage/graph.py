"""确定性 JSON 图与查询接口。

图节点：

* ``base_column``：catalog.schema.table.column 基础列；
* ``row_set``：COUNT(*) 等依赖的基础关系行集标记；
* ``field``：查询/CTE 的输出字段（``relation.name``）。

边类型：

* ``value``：参与值计算；
* ``control``：只影响匹配/选择/分组/窗口排序；
* ``existence``：只影响行集/子结果是否为空（``row_set:`` 标记）。

所有列表按稳定键排序，保证同一 SQL 多次运行产出字节级一致的 JSON。
"""

from __future__ import annotations

from typing import Any

from .model import LineageResult, is_row_set_marker


def build_graph(result: LineageResult) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add_node(node_id: str, kind: str, **extra: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "kind": kind, **extra}

    for field in result.fields:
        field_id = f"{field.relation}.{field.name}"
        add_node(field_id, "field", relation=field.relation, name=field.name)
        for bucket, edge_kind in (
            (field.value_sources, "value"),
            (field.control_sources, "control"),
            (field.existence_sources, "existence"),
        ):
            for src in bucket:
                src_kind = "row_set" if is_row_set_marker(src.ref) else "base_column"
                add_node(src.ref, src_kind)
                edges.append(
                    {
                        "from": src.ref,
                        "to": field_id,
                        "type": edge_kind,
                        "paths": [list(p) for p in sorted(
                            {tuple(p) for p in _paths_of(src)}
                        )],
                    }
                )

    # 同一对节点/类型的边合并路径，再做确定性排序
    merged: dict[tuple[str, str, str], set[tuple[str, ...]]] = {}
    for edge in edges:
        key = (edge["from"], edge["to"], edge["type"])
        merged.setdefault(key, set())
        for path in edge["paths"]:
            merged[key].add(tuple(path))

    final_edges = [
        {
            "from": key[0],
            "to": key[1],
            "type": key[2],
            "paths": [list(p) for p in sorted(paths)],
        }
        for key, paths in merged.items()
    ]
    final_edges.sort(key=lambda e: (e["from"], e["to"], e["type"]))

    return {
        "status": result.status,
        "dialect": result.dialect,
        "output_relation": result.output_relation,
        "nodes": [nodes[k] for k in sorted(nodes)],
        "edges": final_edges,
        "errors": result.errors,
    }


def _paths_of(source: Any) -> list[list[str]]:
    # FieldLineage 序列化后的 dict 或 Source 对象
    if isinstance(source, dict):
        return source.get("paths", [])
    return [list(source.path)]


def field_dependencies(
    result: LineageResult, field_name: str
) -> dict[str, Any]:
    """字段依赖查询：返回某个输出字段的 value/control 基础身份集合。"""
    for field in result.fields:
        if field.name != field_name:
            continue
        return {
            "field": f"{field.relation}.{field.name}",
            "value_sources": _refs(field.value_sources),
            "control_sources": _refs(field.control_sources),
            "existence_sources": _refs(field.existence_sources),
        }
    return {
        "field": field_name,
        "error": f"输出字段不存在；可选: {[f.name for f in result.fields]}",
    }


def downstream_impact(
    result: LineageResult, base_ref: str
) -> dict[str, Any]:
    """基础列/行集变动的下游影响：精确匹配或前缀匹配（整表影响）。"""
    impacted: list[dict[str, Any]] = []
    for field in result.fields:
        value_hits = _matching(field.value_sources, base_ref)
        control_hits = _matching(field.control_sources, base_ref)
        existence_hits = _matching(field.existence_sources, base_ref)
        if not value_hits and not control_hits and not existence_hits:
            continue
        impacted.append(
            {
                "field": f"{field.relation}.{field.name}",
                "impact_types": [
                    kind
                    for kind, hits in (
                        ("value", value_hits),
                        ("control", control_hits),
                        ("existence", existence_hits),
                    )
                    if hits
                ],
                "value_paths": value_hits,
                "control_paths": control_hits,
                "existence_paths": existence_hits,
            }
        )
    impacted.sort(key=lambda d: d["field"])
    return {"changed_ref": base_ref, "impacted_fields": impacted}


def _refs(sources: list) -> list[str]:
    refs = {s.ref if hasattr(s, "ref") else s["ref"] for s in sources}
    return sorted(refs)


def _matching(sources: list, base_ref: str) -> list[list[str]]:
    paths: list[list[str]] = []
    for src in sources:
        ref = src.ref if hasattr(src, "ref") else src["ref"]
        if ref == base_ref or ref.startswith(base_ref + "."):
            if hasattr(src, "path"):
                paths.append(list(src.path))
            else:
                paths.extend(src.get("paths", []))
    return sorted({tuple(p): list(p) for p in paths}.values())
