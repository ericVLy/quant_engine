from django.db import transaction

from apps.execution.models import ExecutionLog, SuiteRun
from apps.suites.models import Suite
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


def rollback_plan(plan, version):
    """Restore a historical snapshot as a new published Plan version."""
    try:
        target = PlanVersion.objects.get(plan=plan, version=version)
    except PlanVersion.DoesNotExist as exc:
        raise PlanError(f'Plan 版本不存在: {version}') from exc

    snapshot = target.snapshot or {}
    if not snapshot.get('root_suite_id'):
        raise PlanError('Plan 历史版本缺少 root_suite_id')
    with transaction.atomic():
        plan = Plan.objects.select_for_update().select_related('root_suite').get(pk=plan.pk)
        root_suite_id = snapshot['root_suite_id']
        if not Suite.objects.filter(pk=root_suite_id, status='published').exists():
            raise PlanError('历史版本的根 Suite 未发布')
        plan.name = snapshot.get('name', plan.name)
        plan.root_suite_id = root_suite_id
        plan.trigger_type = snapshot.get('trigger_type', plan.trigger_type)
        plan.cron_expr = snapshot.get('cron_expr')
        plan.event_type = snapshot.get('event_type')
        plan.symbol_scope = snapshot.get('symbol_scope') or {}
        plan.exec_mode = snapshot.get('exec_mode', plan.exec_mode)
        plan.retry_policy = snapshot.get('retry_policy') or {}
        plan.status = 'published'
        plan.version += 1
        plan.save(update_fields=(
            'name', 'root_suite', 'trigger_type', 'cron_expr', 'event_type',
            'symbol_scope', 'exec_mode', 'retry_policy', 'status', 'version',
            'updated_at',
        ))
        new_snapshot = {
            key: getattr(plan, key)
            for key in (
                'name', 'root_suite_id', 'trigger_type', 'cron_expr',
                'event_type', 'symbol_scope', 'exec_mode', 'retry_policy',
                'status', 'version',
            )
        }
        PlanVersion.objects.create(
            plan=plan, version=plan.version, snapshot=new_snapshot,
        )
        from runner.registry import PlanRegistry
        transaction.on_commit(lambda: PlanRegistry.refresh(plan, force=True))
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
