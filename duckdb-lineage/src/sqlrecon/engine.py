"""对账编排：打开只读连接，逐项执行检查并汇总三类结论。

单项失败（清单/SQL/结构/执行错误）不影响后续检查项；报告里每项有独立
状态，整次命令的退出码区分：

* 0 全部检查通过（结果多重集合一致）；
* 1 至少一项存在真实数据差异（所有检查都成功执行了）；
* 2 至少一项检查无法执行或无法比较（检查错误；可能同时存在数据差异）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compare import ComparisonOutcome, compare
from .errors import ReconError
from .manifest import CheckSpec, load_manifest
from .runner import open_read_only, run_pair

STATUS_EQUAL = "equal"
STATUS_DIFFERENCE = "difference"
STATUS_ERROR = "error"


@dataclass
class CheckReport:
    name: str
    status: str
    description: str
    outcome: ComparisonOutcome | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
        }
        if self.description:
            payload["description"] = self.description
        if self.outcome is not None:
            payload["result"] = self.outcome.to_dict()
        if self.error is not None:
            payload["error"] = self.error
        return payload


def run_checklist(db_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """加载清单并对库文件执行全部检查，返回完整报告字典。"""
    specs: list[CheckSpec] = load_manifest(manifest_path)

    reports: list[CheckReport] = []
    con = open_read_only(db_path)
    try:
        for spec in specs:
            reports.append(_run_one(con, spec))
    finally:
        con.close()

    n_equal = sum(1 for r in reports if r.status == STATUS_EQUAL)
    n_diff = sum(1 for r in reports if r.status == STATUS_DIFFERENCE)
    n_error = sum(1 for r in reports if r.status == STATUS_ERROR)

    if n_error:
        overall = STATUS_ERROR
    elif n_diff:
        overall = STATUS_DIFFERENCE
    else:
        overall = STATUS_EQUAL

    return {
        "database": str(db_path),
        "manifest": str(manifest_path),
        "overall_status": overall,
        "summary": {
            "checks_total": len(reports),
            "equal": n_equal,
            "difference": n_diff,
            "error": n_error,
        },
        "checks": [r.to_dict() for r in reports],
    }


def _run_one(con, spec: CheckSpec) -> CheckReport:
    try:
        old_result, new_result = run_pair(con, spec.old_sql, spec.new_sql)
        outcome = compare(spec, old_result, new_result)
    except ReconError as ex:
        # 名称/类型/语句限制/执行错误与数据差异严格分开。
        return CheckReport(
            name=spec.name,
            status=STATUS_ERROR,
            description=spec.description,
            error=ex.to_payload(),
        )
    return CheckReport(
        name=spec.name,
        status=STATUS_EQUAL if outcome.equal else STATUS_DIFFERENCE,
        description=spec.description,
        outcome=outcome,
    )


def exit_code_for(report: dict[str, Any]) -> int:
    return {
        STATUS_EQUAL: 0,
        STATUS_DIFFERENCE: 1,
        STATUS_ERROR: 2,
    }[report["overall_status"]]
