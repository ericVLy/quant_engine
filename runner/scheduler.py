from datetime import datetime

from apps.watchlists.services import resolve_symbol_scope

from .queue import TaskQueue
from .registry import PlanRegistry


class Scheduler:
    """Polling scheduler for published time-triggered plans."""

    def __init__(self, task_queue=None):
        self.task_queue = task_queue or TaskQueue()
        self._enqueued = set()

    def due_plans(self, now):
        """从注册中心（热加载）读取已发布的时间驱动 Plan，命中 Cron 者返回。"""
        return [plan for plan in PlanRegistry.published_plans()
                if plan.trigger_type == 'time' and self._matches_cron(plan.cron_expr, now)]

    @staticmethod
    def _matches_cron(expression, value):
        if not expression:
            return False
        fields = expression.split()
        if len(fields) != 5:
            return False
        values = [value.minute, value.hour, value.day, value.month, value.weekday()]
        return all(Scheduler._matches_field(field, current)
                   for field, current in zip(fields, values))

    @staticmethod
    def _matches_field(field, value):
        for part in field.split(','):
            try:
                base, _, step_text = part.partition('/')
                step = int(step_text) if step_text else 1
                if step < 1:
                    return False
                if base in ('', '*'):
                    if value % step == 0:
                        return True
                    continue
                if '-' in base:
                    start, end = (int(item) for item in base.split('-', 1))
                    if start <= value <= end and (value - start) % step == 0:
                        return True
                    continue
                if step == 1 and value == int(base):
                    return True
            except (TypeError, ValueError):
                return False
        return False

    def enqueue_due_plans(self, now):
        enqueued = 0
        for plan in self.due_plans(now):
            for symbol in resolve_symbol_scope(plan.symbol_scope):
                key = (plan.pk, plan.version, symbol.code, now.year, now.month, now.day, now.hour, now.minute)
                if key in self._enqueued:
                    continue
                self.task_queue._queue.put_nowait((plan, symbol.code, {}))
                self._enqueued.add(key)
                enqueued += 1
        return self.task_queue

    def poll_once(self, now=None):
        """Poll published time plans once; repeated polls in one minute are idempotent."""
        return self.enqueue_due_plans(now or datetime.now())