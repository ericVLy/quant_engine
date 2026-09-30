# pylint: disable=too-many-return-statements  # 多分支早返回（校验 / 查表 / 降级链）比深嵌套更易读
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
import asyncio
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from threading import Event

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from apps.plans.services import resolve_plan_symbols

from .queue import TaskQueue
from .registry import PlanRegistry

logger = logging.getLogger(__name__)

#: 去重键上限：超过后只保留"当天"的键，避免长驻进程内存无限增长。
ENQUEUED_KEY_LIMIT = 2000

#: 持久化执行意向（pending 运行）的默认有效期（秒）：超时不再投递，由恢复器收口。
DEFAULT_PENDING_MAX_AGE = 300

#: 补投 pending 意向的最小滞留时长（秒）：避免与同轮「即时投递」重复竞争。
DEFAULT_PENDING_SWEEP_MIN_AGE = 5


def _setting(name, default):
    """读取 settings 覆盖值（缺失或为 ``None`` 时用默认）。"""
    value = getattr(settings, name, None)
    return default if value is None else value


def _as_aware(moment):
    """把 naive 的本地墙钟时间换算为**同一绝对时刻**的 aware 时间。

    ``run_forever`` 的默认 ``clock`` 是 ``datetime.now``（naive 本地时间），而
    ``purge_execution_history`` 需要 aware 时间与库内 aware 字段比较。直接
    ``make_aware`` 会按 ``settings.TIME_ZONE`` 解释墙钟，服务器本地时区与
    ``TIME_ZONE`` 不一致时（如本地 +08 / ``TIME_ZONE='UTC'``）会整体偏移一个
    时区差；这里改用系统本地 UTC 偏移换算绝对时刻。aware 入参原样返回。
    """
    if timezone.is_aware(moment):
        return moment
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return (moment - offset).replace(tzinfo=dt_timezone.utc)


class Scheduler:
    """Polling scheduler for published time-triggered plans."""

    def __init__(self, task_queue=None, poll_interval=60,
                 pending_max_age=None, pending_sweep_min_age=None):
        self.task_queue = task_queue or TaskQueue()
        if poll_interval <= 0:
            raise ValueError('poll_interval 必须大于 0')
        self.poll_interval = poll_interval
        self._enqueued = set()
        self._stop_event = Event()
        self._last_purge_date = None
        self._enqueued_limit = ENQUEUED_KEY_LIMIT
        # 执行意向的时效与补投门槛（见 materialize_due_tasks / collect_pending_tasks）
        self.pending_max_age = (
            _setting('EXECUTION_PENDING_MAX_AGE_SECONDS', DEFAULT_PENDING_MAX_AGE)
            if pending_max_age is None else pending_max_age)
        self.pending_sweep_min_age = (
            _setting('EXECUTION_PENDING_SWEEP_MIN_AGE_SECONDS', DEFAULT_PENDING_SWEEP_MIN_AGE)
            if pending_sweep_min_age is None else pending_sweep_min_age)

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

    def _prune_enqueued(self, now):
        """限制去重键规模：超限后只保留"当天"的键，避免长驻进程内存无限增长。"""
        if len(self._enqueued) <= self._enqueued_limit:
            return
        today = (now.year, now.month, now.day)
        self._enqueued = {
            key for key in self._enqueued
            if (key[3], key[4], key[5]) == today
        }

    def collect_due_tasks(self, now):
        """同步：刷新注册中心并返回本轮到期任务 ``[(plan, symbol_code)]``（不入队）。

        单独抽出来的原因：``asyncio.Queue`` 跨线程 ``put`` / ``put_nowait`` 不安全
        （唤醒逻辑必须回到事件循环线程），所以异步调度器把 **DB 读取放在同步线程**、
        **入队留在事件循环线程**；本方法即前者。

        Args:
            now: 当前时间（用于 cron 匹配与去重键）。

        Returns:
            list[tuple]: 本轮新增的 ``(Plan 实例, 标的代码)``；同一
            ``(Plan, version, 标的, 年, 月, 日, 时, 分)`` 只出现一次。
        """
        PlanRegistry.sync_from_database()
        tasks = []
        for plan in self.due_plans(now):
            # 标的集合来自 Case 声明（编排树内并集），Plan 自身不再持有 symbol_scope
            for symbol in resolve_plan_symbols(plan):
                key = (plan.pk, plan.version, symbol.code,
                       now.year, now.month, now.day, now.hour, now.minute)
                if key in self._enqueued:
                    continue
                self._enqueued.add(key)
                tasks.append((plan, symbol.code))
        self._prune_enqueued(now)
        return tasks

    def materialize_due_tasks(self, now):
        """同步：把本轮到期任务**落库**为持久化执行意向，返回待投递任务。

        数据表链路：每个 ``(Plan, 标的)`` 任务落成一条 ``pending`` ``SuiteRun``
        （含 ``SUITE_INIT`` 事件）。进程重启后意向仍在库里，不会像内存队列那样
        直接消失；DB 层再按「同 ``(Plan, 标的)`` 是否已有未结束运行」去重，因此
        **重启后同一分钟重复轮询也不会产生第二条运行**（内存 ``_enqueued`` 只
        在同进程内有效）。

        Args:
            now: 当前时间（cron 匹配与去重键）。

        Returns:
            list[tuple]: ``[(plan, symbol_code, {'suite_run_id': pk})]``。
            已有未结束运行时不新建，直接复用其主键；重复投递由
            :func:`apps.execution.services.claim_suite_run` 的原子认领兜底。
        """
        from apps.execution.services import create_suite_run, find_active_run

        tasks = []
        for plan, symbol in self.collect_due_tasks(now):
            existing = find_active_run(plan, symbol)
            if existing is not None:
                if existing.status == 'pending' and not self._is_fresh(existing, now):
                    logger.info(
                        '同 (Plan, 标的) 已有过期 pending 意向，不再投递：run=%s', existing.pk)
                    continue
                tasks.append((plan, symbol, {'suite_run_id': existing.pk}))
                continue
            run = create_suite_run(plan, symbol)
            tasks.append((plan, symbol, {'suite_run_id': run.pk}))
        return tasks

    def collect_pending_tasks(self, now):
        """同步：补投「已落库但尚未被认领」的 pending 意向。

        覆盖两类来源：① MCP ``trigger_plan_execution`` 创建的执行意向；
        ② 上次进程退出时已投递、但随内存队列丢失的意向。只投递**新鲜**且已滞留
        超过 ``pending_sweep_min_age`` 秒的意向（避免与同轮即时投递重复竞争）；
        过期意向不再投递，由 :func:`apps.execution.recovery.recover_orphaned_runs`
        收口为 ``PENDING_EXPIRED``。

        Args:
            now: 当前时间（naive 墙钟会自动换算为 aware 再查库）。

        Returns:
            list[tuple]: ``[(plan, symbol_code, {'suite_run_id': pk})]``，
            同 ``(Plan, 标的)`` 只保留最早一条。
        """
        from apps.execution.models import SuiteRun

        aware_now = _as_aware(now)
        threshold = aware_now - timedelta(seconds=self.pending_max_age)
        settled = aware_now - timedelta(seconds=self.pending_sweep_min_age)
        runs = (SuiteRun.objects
                .filter(status='pending', created_at__gte=threshold, created_at__lte=settled)
                .select_related('plan').order_by('created_at', 'pk'))
        tasks, seen = [], set()
        for run in runs:
            if run.plan is None or run.plan.status != 'published':
                continue
            key = (run.plan_id, run.symbol)
            if key in seen:
                continue
            seen.add(key)
            tasks.append((run.plan, run.symbol, {'suite_run_id': run.pk}))
        return tasks

    def due_tasks(self, now):
        """同步：本轮全部待投递任务（到期落库意向 + 遗留 pending 补投，按运行去重）。"""
        tasks = self.materialize_due_tasks(now)
        known = {payload['suite_run_id'] for _, _, payload in tasks}
        for item in self.collect_pending_tasks(now):
            if item[2]['suite_run_id'] not in known:
                tasks.append(item)
        return tasks

    def _is_fresh(self, run, now):
        """``run`` 是否仍在 ``pending_max_age`` 有效期内。"""
        return run.created_at >= _as_aware(now) - timedelta(seconds=self.pending_max_age)

    def enqueue_due_plans(self, now):
        """把本轮任务（含遗留 pending 意向补投）投递到 ``TaskQueue``。"""
        for plan, symbol, payload in self.due_tasks(now):
            self.task_queue.put_nowait(plan, symbol, payload)
        return self.task_queue

    def poll_once(self, now=None):
        """Poll published time plans once; repeated polls in one minute are idempotent."""
        return self.enqueue_due_plans(now or datetime.now())

    def stop(self):
        """请求常驻调度循环在当前轮询结束后退出。"""
        self._stop_event.set()

    def run_forever(self, stop_event=None, clock=datetime.now):
        """持续刷新数据库配置并轮询 Cron，直到收到停止请求。

        同时在每轮执行一次「执行日志生命周期清理」门禁（每日至多一次，见 N-04），
        清理失败只记日志、不影响调度。

        注意：仅投递任务到 ``TaskQueue``，**不消费**；需配合
        :meth:`run_forever_async` + :class:`~runner.queue.WorkerPool` 才有执行端。
        """
        event = stop_event or self._stop_event
        while not event.is_set():
            now = clock()
            self.poll_once(now)
            self._maybe_purge_logs(now)
            event.wait(self.poll_interval)

    async def run_forever_async(self, stop_event=None, clock=datetime.now):
        """异步常驻轮询：DB 读取走同步线程，任务入队留在事件循环线程。

        与 :meth:`run_forever` 的分工：本方法负责**生产**任务（投递到
        ``self.task_queue``），消费由 ``WorkerPool.run_forever`` 在同一事件循环内完成。

        - ``collect_due_tasks``（含 ORM / PlanRegistry）经 ``sync_to_async``
          在同步线程执行，避免在事件循环里访问 Django ORM；
        - ``await self.task_queue.put(...)`` 在事件循环线程执行，
          因为 ``asyncio.Queue`` 跨线程入队会导致等待方唤醒不可靠；
        - 停止条件：``stop_event``（``asyncio.Event``）置位或 :meth:`stop` 被调用；
          等待用 ``asyncio.wait_for`` 以便停止可立即生效（不等满一个轮询周期）。

        Args:
            stop_event: ``asyncio.Event``；缺省时只响应 :meth:`stop`。
            clock: 取当前时间的可调用对象（默认 ``datetime.now``）。
        """
        stop = stop_event or asyncio.Event()
        while not stop.is_set() and not self._stop_event.is_set():
            now = clock()
            tasks = await sync_to_async(self.due_tasks, thread_sensitive=True)(now)
            for plan, symbol, payload in tasks:
                await self.task_queue.put(plan, symbol, payload)
            await sync_to_async(self._maybe_purge_logs, thread_sensitive=True)(now)
            if stop.is_set() or self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                continue

    def _maybe_purge_logs(self, now):
        """每日一次清理过期执行痕迹（N-04）；受 settings 开关控制。

        ``now`` 通常是 ``run_forever`` 的 ``clock()``（默认 ``datetime.now``，
        **naive 本地墙钟**）：cron 匹配与「每日一次」判定需要墙钟语义，但
        ``purge_execution_history`` 会拿它与 aware 字段比较，naive 值会触发
        Django 告警并按当前时区误解释，使 30 天保留边界整体漂移
        （``TIME_ZONE='UTC'`` + 本地 +08 时偏移 8 小时）。故这里先把 naive
        墙钟换算成**同一绝对时刻**的 aware 时间再下传。
        """
        if not getattr(settings, 'EXECUTION_LOG_RETENTION_ENABLED', True):
            return None
        today = now.date()
        if self._last_purge_date == today:
            return None
        from apps.execution.retention import purge_execution_history

        try:
            stats = purge_execution_history(now=_as_aware(now))
        except Exception:  # pylint: disable=broad-except
            logger.exception('执行日志清理失败，下一轮重试')
            return None
        self._last_purge_date = today
        return stats
