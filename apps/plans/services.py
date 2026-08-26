from django.db import transaction

from apps.execution.models import ExecutionLog, SuiteRun
from apps.watchlists.services import resolve_symbol_scope

from .models import Plan


class PlanError(Exception):
    """Raised when a Plan cannot be published or triggered."""


def publish_plan(plan):
    """Publish a Plan only when its root Suite is already published."""
    if plan.root_suite.status != 'published':
        raise PlanError('根 Suite 必须已发布')
    plan.status = 'published'
    plan.version += 1
    plan.save(update_fields=('status', 'version', 'updated_at'))
    return plan


def resolve_plan_symbols(plan):
    """Resolve and return symbols covered by a Plan."""
    return resolve_symbol_scope(plan.symbol_scope)


def delete_plan(plan):
    """Delete a Plan only when it has no execution history."""
    if SuiteRun.objects.filter(plan=plan).exists():
        raise PlanError('Plan 已有执行记录，不能删除')
    if ExecutionLog.objects.filter(plan=plan).exists():
        raise PlanError('Plan 已有执行日志，不能删除')
    plan.delete()
