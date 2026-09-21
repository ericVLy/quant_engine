"""Case 业务服务：删除保护等跨模型约束（REST 与 MCP 共用同一处判定）。"""


class CaseError(Exception):
    """Raised when a Case cannot be changed or deleted."""


def delete_case(case):
    """Delete a Case only when no Suite references it.

    Args:
        case: ``cases.models.Case`` 实例。

    Raises:
        CaseError: Case 已被 Suite 引用（REST 语义 409 Conflict）。
    """
    if case.suites.exists():
        raise CaseError('Case 已被 Suite 引用，不能删除')
    case.delete()
