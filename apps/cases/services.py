"""Case 业务服务：删除保护与发布（REST 与 MCP 共用同一处判定）。"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from django.db import transaction

from .models import CaseVersion


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


@transaction.atomic
def publish_case(case):
    """发布 Case：校验参数白名单与深层语义，递增版本并固化发布快照。

    Args:
        case: ``cases.models.Case`` 实例。

    Returns:
        Case: 发布后的 Case（``status='published'``，``version`` 已 +1，
        并新增一条 ``CaseVersion`` 快照）。

    Raises:
        rest_framework.serializers.ValidationError: ``params`` 不满足
            ``validate_case_schema``（白名单 / 指标目录 / 阈值区间等）。
    """
    from .serializers import validate_case_schema

    validate_case_schema(case.node_type, case.params or {})
    case.status = 'published'
    case.version += 1
    case.save(update_fields=('status', 'version', 'updated_at'))
    CaseVersion.objects.create(
        case=case,
        version=case.version,
        name=case.name,
        node_type=case.node_type,
        params=case.params,
        status=case.status,
    )
    return case
