"""关系数据裁取的错误模型。

错误分三层，报告与退出码都依赖这个区分：

* 配方/源结构问题（:class:`RecipeError` / :class:`SourceSchemaError`）——
  配方 JSON 形状不合法、配方引用了源库中不存在的表/列、身份列类型不合规、
  身份在源表中不唯一或含空值、引用两侧列数/类型不符等。全部带 ``location``
  字段指出具体位置（如 ``references[2].child_columns[1]``），不默默忽略。
* 裁取失败（:class:`SeedNotFoundError` / :class:`BrokenReferenceError` /
  :class:`BudgetExceededError`）——配方与源结构都没问题，但按规则追踪时
  找不到起始身份、选中记录引用了不存在的父记录、或必需记录超出预算。
  这类失败与数据内容有关，退出码与配方错误分开。
* 命令级问题（:class:`DatabaseOpenError` / :class:`TargetExistsError`）——
  源库打不开、目标文件已存在等。

所有异常都带稳定 ``code``，由 CLI 收口为退出码与 JSON 错误载荷。
"""

from __future__ import annotations


class SliceError(Exception):
    """所有裁取工具错误的基类，携带稳定的机器可读 code。"""

    code = "slice_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class RecipeError(SliceError):
    """配方 JSON 本身的结构或取值不合法（形状问题，不依赖源库内容）。"""

    code = "recipe_invalid"


class SourceSchemaError(SliceError):
    """配方与源库对不上：缺表/缺列、身份列类型不合规、身份不唯一或含空值、
    引用两侧列数或类型不一致。"""

    code = "source_schema_error"


class DatabaseOpenError(SliceError):
    """源数据库文件无法以只读方式打开。"""

    code = "database_open_error"


class TargetExistsError(SliceError):
    """目标文件已存在；裁取绝不覆盖已有内容。"""

    code = "target_exists"


class SeedNotFoundError(SliceError):
    """起始身份在源表中找不到，整次裁取失败。"""

    code = "seed_not_found"


class BrokenReferenceError(SliceError):
    """已选中的记录引用了不存在的父记录；载荷带从起点到断口的完整链路。"""

    code = "broken_reference"


class BudgetExceededError(SliceError):
    """追踪过程中确认必需记录数超过预算；不截断、不产出残缺样本。"""

    code = "budget_exceeded"
