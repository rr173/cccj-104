"""结果对账的错误模型。

检查失败分两大类，报告与退出码都依赖这个区分：

* :class:`ReconCheckError` —— 检查 *无法执行或无法比较*：清单格式、名称/类型
  不符、SQL 安全限制、DuckDB 执行报错等。这不是"数据有差异"，而是这项检查
  本身不成立（code 以 ``check_`` / ``sql_`` / ``schema_`` 开头）。
* 数据差异由 :mod:`sqlrecon.compare` 直接体现在报告里（``status=difference``），
  不使用异常。

设计原则：名称、类型、语句限制或执行错误必须与真实数据差异分开报告；一项
失败不能阻断清单里的其他项，因此所有异常都带稳定 ``code``，由 CLI 收口。
"""

from __future__ import annotations


class ReconError(Exception):
    """所有对账工具错误的基类，携带稳定的机器可读 code。"""

    code = "recon_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class ManifestError(ReconError):
    """检查清单本身的结构或取值不合法。"""

    code = "check_manifest_invalid"


class SQLError(ReconError):
    """查询文本不满足"单条只读查询"约束（解析/形态/不安全特性）。"""

    code = "sql_rejected"


class SchemaMismatchError(ReconError):
    """两份查询的输出列在名称、顺序或 DuckDB 逻辑类型上不一致。"""

    code = "schema_mismatch"


class ExecutionError(ReconError):
    """查询通过了静态检查，但在 DuckDB 实际执行时失败。"""

    code = "sql_execution_error"


class DatabaseOpenError(ReconError):
    """数据库文件无法以只读方式打开。"""

    code = "database_open_error"
