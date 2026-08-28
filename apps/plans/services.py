from django.db import transaction

from apps.execution.models import ExecutionLog, SuiteRun
from apps.watchlists.services import resolve_symbol_scope

from .models import Plan
from .models import PlanVersion


class PlanError(Exception):
    """Raised when a Plan cannot be published or triggered."""


def publish_plan(plan):
    """Publish a Plan only when its root Suite is already published."""
    if plan.root_suite.status != 'published':
        raise PlanError('根 Suite 必须已发布')
    from runner.registry import PlanRegistry

    with transaction.atomic():
        plan.status = 'published'
        plan.version += 1
        plan.save(update_fields=('status', 'version', 'updated_at'))
        snapshot = {
            'name': plan.name,
            'root_suite_id': plan.root_suite_id,
            'trigger_type': plan.trigger_type,
            'cron_expr': plan.cron_expr,
            'event_type': plan.event_type,
            'symbol_scope': plan.symbol_scope,
            'exec_mode': plan.exec_mode,
            'retry_policy': plan.retry_policy,
            'status': plan.status,
            'version': plan.version,
        }
        PlanVersion.objects.create(plan=plan, version=plan.version, snapshot=snapshot)
        transaction.on_commit(lambda: PlanRegistry.refresh(plan))
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
