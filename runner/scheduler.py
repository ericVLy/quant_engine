import logging
from datetime import datetime
from threading import Event

from django.conf import settings

from apps.watchlists.services import resolve_symbol_scope

from .queue import TaskQueue
from .registry import PlanRegistry

logger = logging.getLogger(__name__)


class Scheduler:
    """Polling scheduler for published time-triggered plans."""

    def __init__(self, task_queue=None, poll_interval=60):
        self.task_queue = task_queue or TaskQueue()
        if poll_interval <= 0:
            raise ValueError('poll_interval 必须大于 0')
        self.poll_interval = poll_interval
        self._enqueued = set()
        self._stop_event = Event()
        self._last_purge_date = None

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
        values = [value.minute, value.hour, value.day, value.month,
                  (value.weekday() + 1) % 7]
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
        return all(Scheduler._matches_field(field, current, *limit)
                   for field, current, limit in zip(fields, values, bounds))

    @staticmethod
    def _matches_field(field, value, minimum=None, maximum=None):
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
                    if minimum is not None and (start < minimum or end > maximum or start > end):
                        return False
                    if minimum == 0 and maximum == 7 and value == 0 and end == 7:
                        current = 7
                    else:
                        current = value
                    if start <= current <= end and (current - start) % step == 0:
                        return True
                    continue
                point = int(base)
                if minimum is not None and not minimum <= point <= maximum:
                    return False
                is_sunday_alias = minimum == 0 and maximum == 7 and value == 0 and point == 7
                if step == 1 and (value == point or is_sunday_alias):
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
                self.task_queue.put_nowait(plan, symbol.code)
                self._enqueued.add(key)
                enqueued += 1
        return self.task_queue

    def poll_once(self, now=None):
        """Poll published time plans once; repeated polls in one minute are idempotent."""
        PlanRegistry.sync_from_database()
        return self.enqueue_due_plans(now or datetime.now())

    def stop(self):
        """请求常驻调度循环在当前轮询结束后退出。"""
        self._stop_event.set()

    def run_forever(self, stop_event=None, clock=datetime.now):
        """持续刷新数据库配置并轮询 Cron，直到收到停止请求。

        同时在每轮执行一次「执行日志生命周期清理」门禁（每日至多一次，见 N-04），
        清理失败只记日志、不影响调度。
        """
        event = stop_event or self._stop_event
        while not event.is_set():
            now = clock()
            self.poll_once(now)
            self._maybe_purge_logs(now)
            event.wait(self.poll_interval)

    def _maybe_purge_logs(self, now):
        """每日一次清理过期执行痕迹（N-04）；受 settings 开关控制。"""
        if not getattr(settings, 'EXECUTION_LOG_RETENTION_ENABLED', True):
            return None
        today = now.date()
        if self._last_purge_date == today:
            return None
        from apps.execution.retention import purge_execution_history

        try:
            stats = purge_execution_history(now=now)
        except Exception:  # pylint: disable=broad-except
            logger.exception('执行日志清理失败，下一轮重试')
            return None
        self._last_purge_date = today
        return stats