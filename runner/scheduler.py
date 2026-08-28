from apps.plans.models import Plan
from apps.watchlists.services import resolve_symbol_scope

from .queue import TaskQueue


class Scheduler:
    """Polling scheduler for published time-triggered plans."""

    def __init__(self, task_queue=None):
        self.task_queue = task_queue or TaskQueue()

    def due_plans(self, now):
        plans = Plan.objects.filter(status='published', trigger_type='time')
        return [plan for plan in plans if self._matches_cron(plan.cron_expr, now)]

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
        for plan in self.due_plans(now):
            for symbol in resolve_symbol_scope(plan.symbol_scope):
                self.task_queue._queue.put_nowait((plan, symbol.code, {}))
        return self.task_queue