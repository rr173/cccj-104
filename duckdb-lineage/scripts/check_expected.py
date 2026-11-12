"""第二步验证：把抽取结果与手工标注的期望依赖集逐项核对。

断言类型：
  value                     value_sources 的 ref 集合必须精确相等
  control_contains          control_sources 必须包含给定 ref（匹配/选择原因）
  existence_contains        existence_sources 必须包含给定 ref（行集为空原因）
  value_row_sets            value 中的行集标记集合精确相等
  existence_row_sets        existence 中的行集标记集合精确相等
  forbidden_value_contains  value_sources 不得包含给定 ref
  forbidden_control_contains  control_sources 不得包含给定 ref（角色混淆防线）
  forbidden_existence_contains existence_sources 不得包含给定 ref
  forbidden_any_contains    三个桶都不得包含给定 ref（占位投影不得成为原因）
  forbidden_value_prefix    value_sources 的 ref 不得有该前缀（防拼接假边）
  expected_error_code / expected_location  错误码与 [line, col] 定位

运行：python3 -I scripts/check_expected.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import site  # noqa: E402

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

from lineage.analyzer import LineageAnalyzer  # noqa: E402
from lineage.catalog import Catalog  # noqa: E402


def _refs(field: dict, bucket: str) -> set[str]:
    return {s["ref"] for s in field.get(bucket, [])}


def check() -> int:
    catalog = Catalog.load(ROOT / "catalog" / "catalog.json")
    analyzer = LineageAnalyzer(catalog)
    expected = json.loads(
        (ROOT / "expected" / "expected_lineage.json").read_text(encoding="utf-8")
    )

    failures: list[str] = []
    checked = 0
    for sql_name, spec in expected.items():
        checked += 1
        sql = (ROOT / "sql" / sql_name).read_text(encoding="utf-8")
        result = analyzer.analyze(sql, sql_file=sql_name)
        payload = result.to_dict()

        if payload["status"] != spec["expected_status"]:
            failures.append(
                f"{sql_name}: status 期望 {spec['expected_status']}，"
                f"实际 {payload['status']} ({payload['errors']})"
            )
            continue

        if "expected_error_code" in spec:
            codes = [e["code"] for e in payload["errors"]]
            if spec["expected_error_code"] not in codes:
                failures.append(
                    f"{sql_name}: 错误码期望 {spec['expected_error_code']}，实际 {codes}"
                )
            if "expected_location" in spec:
                loc = payload["errors"][0].get("location")
                got = [loc["line"], loc["col"]] if loc else None
                if got != spec["expected_location"]:
                    failures.append(
                        f"{sql_name}: 定位期望 {spec['expected_location']}，实际 {got}"
                    )
            continue

        if "expected_field_count" in spec:
            if len(payload["fields"]) != spec["expected_field_count"]:
                failures.append(
                    f"{sql_name}: 字段数期望 {spec['expected_field_count']}，"
                    f"实际 {len(payload['fields'])}"
                )

        actual = {f["name"]: f for f in payload["fields"]}
        for field_name, rules in spec.get("fields", {}).items():
            if field_name not in actual:
                failures.append(f"{sql_name}: 缺少输出字段 {field_name}")
                continue
            field = actual[field_name]
            value = _refs(field, "value_sources")
            control = _refs(field, "control_sources")
            existence = _refs(field, "existence_sources")

            if "value" in rules and value != set(rules["value"]):
                failures.append(
                    f"{sql_name}.{field_name}: value 期望 {sorted(rules['value'])}，"
                    f"实际 {sorted(value)}"
                )
            for ref in rules.get("control_contains", []):
                if ref not in control:
                    failures.append(
                        f"{sql_name}.{field_name}: control 缺少 {ref}，实际 {sorted(control)}"
                    )
            for ref in rules.get("existence_contains", []):
                if ref not in existence:
                    failures.append(
                        f"{sql_name}.{field_name}: existence 缺少 {ref}，"
                        f"实际 {sorted(existence)}"
                    )
            if "value_row_sets" in rules:
                row_sets = {r for r in value if r.startswith("row_set:")}
                if row_sets != set(rules["value_row_sets"]):
                    failures.append(
                        f"{sql_name}.{field_name}: 行集标记期望 "
                        f"{sorted(rules['value_row_sets'])}，实际 {sorted(row_sets)}"
                    )
            if "existence_row_sets" in rules:
                row_sets = {
                    r for r in existence if r.startswith("row_set:")
                }
                if row_sets != set(rules["existence_row_sets"]):
                    failures.append(
                        f"{sql_name}.{field_name}: existence 行集标记期望 "
                        f"{sorted(rules['existence_row_sets'])}，"
                        f"实际 {sorted(row_sets)}"
                    )
            for ref in rules.get("forbidden_value_contains", []):
                if ref in value:
                    failures.append(
                        f"{sql_name}.{field_name}: value 不应包含 {ref}"
                    )
            for ref in rules.get("forbidden_control_contains", []):
                if ref in control:
                    failures.append(
                        f"{sql_name}.{field_name}: control 不应包含 {ref}"
                        "（value/control 角色混淆）"
                    )
            for ref in rules.get("forbidden_existence_contains", []):
                if ref in existence:
                    failures.append(
                        f"{sql_name}.{field_name}: existence 不应包含 {ref}"
                    )
            for ref in rules.get("forbidden_any_contains", []):
                if ref in value or ref in control or ref in existence:
                    failures.append(
                        f"{sql_name}.{field_name}: 任何来源都不应包含 {ref}"
                        "（占位投影/无关列不得成为影响原因）"
                    )
            for prefix in rules.get("forbidden_value_prefix", []):
                bad = [r for r in value if r.startswith(prefix)]
                if bad:
                    failures.append(
                        f"{sql_name}.{field_name}: value 出现拼接假边 {bad}"
                    )

    print(f"核对样例数: {checked}")
    if failures:
        print(f"失败断言数: {len(failures)}")
        for msg in failures:
            print(f"  [FAIL] {msg}")
        return 1
    print("全部手工标注断言通过：value/control/existence 分桶、行集标记、错误定位均符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
