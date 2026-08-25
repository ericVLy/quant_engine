from django.db import transaction
from django.utils import timezone

from apps.plans.models import Plan
from apps.suites.models import Edge

from .events import EventType
from .models import Event, SuiteRun
from .registry import EventRegistry


class ExecutionError(Exception):
    """Raised when an execution lifecycle operation is invalid."""


def create_suite_run(plan, symbol, initial_payload=None):
    """Create a pending run and enqueue its initialization event."""
    if plan.status != 'published':
        raise ExecutionError('只有已发布的 Plan 才能触发执行')
    if not symbol:
        raise ExecutionError('symbol 不能为空')

    with transaction.atomic():
        run = SuiteRun.objects.create(
            plan=plan,
            suite=plan.root_suite,
            symbol=symbol,
            status='pending',
            event_queue=[],
        )
        enqueue_event(run, EventType.SUITE_INIT, source='plan', payload=initial_payload or {})
    return run


def start_suite_run(run):
    """Move a pending run to running and enqueue SUITE_START once."""
    if run.status != 'pending':
        raise ExecutionError(f'运行 {run.pk} 当前状态为 {run.status}，不能启动')

    run.status = 'running'
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at'])
    enqueue_event(run, EventType.SUITE_START, source='runner')
    return run


def stop_suite_run(run):
    """Stop a run unless it has already reached a terminal state."""
    if run.status in ('completed', 'failed', 'stopped'):
        raise ExecutionError(f'运行 {run.pk} 已处于终态 {run.status}')

    run.status = 'stopped'
    run.ended_at = timezone.now()
    run.save(update_fields=['status', 'ended_at'])
    return run


def enqueue_event(run, event_type, source='', payload=None):
    """Persist an event and append its id to the run queue."""
    if not EventRegistry.validate(event_type):
        raise ExecutionError(f'未注册的事件类型: {event_type}')

    event = Event.objects.create(
        run=run,
        event_type=event_type,
        source=source,
        payload=payload or {},
        status='pending',
    )
    run.event_queue = [*run.event_queue, event.pk]
    run.save(update_fields=['event_queue'])
    return event


def _event_condition_matches(condition, payload):
    """Match simple equality conditions used by Suite edges."""
    if not condition:
        return True
    return all(payload.get(key) == value for key, value in condition.items())


def process_next_event(run):
    """Process the oldest queued event and return it, or None when empty."""
    if run.status not in ('running', 'pending'):
        raise ExecutionError(f'运行 {run.pk} 当前状态为 {run.status}，不能处理事件')
    if not run.event_queue:
        return None

    event_id = run.event_queue[0]
    event = Event.objects.get(pk=event_id, run=run)
    event.status = 'processing'
    event.save(update_fields=['status'])

    try:
        matching_edges = Edge.objects.filter(from_suite=run.suite)
        for edge in matching_edges:
            if _event_condition_matches(edge.event_condition, event.payload):
                enqueue_event(
                    run,
                    edge.event_condition.get('next_event', EventType.CASE_START),
                    source=f'edge:{edge.pk}',
                    payload=event.payload,
                )
        event.status = 'done'
        event.processed_at = timezone.now()
        event.save(update_fields=['status', 'processed_at'])
        run.event_queue = run.event_queue[1:]
        run.save(update_fields=['event_queue'])
        return event
    except Exception:
        event.status = 'failed'
        event.processed_at = timezone.now()
        event.save(update_fields=['status', 'processed_at'])
        run.status = 'failed'
        run.ended_at = timezone.now()
        run.save(update_fields=['status', 'ended_at'])
        raise


def complete_suite_run(run):
    """Complete a running run after its queue has been drained."""
    if run.status != 'running':
        raise ExecutionError(f'运行 {run.pk} 当前状态为 {run.status}，不能完成')
    if run.event_queue:
        raise ExecutionError('事件队列未清空，不能完成运行')

    run.status = 'completed'
    run.ended_at = timezone.now()
    run.save(update_fields=['status', 'ended_at'])
    return run


def trigger_plan(plan_id, symbols, payload=None):
    """Create one pending SuiteRun for each requested symbol."""
    plan = Plan.objects.select_related('root_suite').get(pk=plan_id)
    return [create_suite_run(plan, symbol, payload) for symbol in symbols]
