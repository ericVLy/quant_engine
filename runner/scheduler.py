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
        if field == '*':
            return True
        try:
            return value in {int(part) for part in field.split(',')}
        except ValueError:
            return False

    def enqueue_due_plans(self, now):
        for plan in self.due_plans(now):
            for symbol in resolve_symbol_scope(plan.symbol_scope):
                self.task_queue._queue.put_nowait((plan, symbol.code, {}))
        return self.task_queue