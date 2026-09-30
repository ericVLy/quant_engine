"""TaskQueue 消费端测试（Scheduler 生产 → WorkerPool 消费 → 真实执行链）。

覆盖本次修复的核心：
- :meth:`runner.queue.WorkerPool.run_forever`（常驻消费、失败不中断、停止即退）；
- :meth:`runner.scheduler.Scheduler.collect_due_tasks`（DB 侧取任务 + 去重 + 键裁剪）；
- :meth:`runner.scheduler.Scheduler.run_forever_async`（异步轮询：DB 走同步线程、入队留在事件循环线程）；
- :class:`runner.service.PlanExecutionService`（按主键重读 Plan、跳过非 published、
  执行落库、失败补发告警）与 :func:`runner.service.build_execution_service` 装配。

线程注意：``arun`` / ``run_forever_async`` 内部用 ``sync_to_async`` 在**另一个线程**访问 ORM，
Django ``TestCase`` 的事务对其他连接不可见，所以跨线程的用例用 ``TransactionTestCase``
（数据已提交）。不跨线程的用例仍用 ``TestCase``。
"""
# pylint: disable=protected-access  # 测试需访问私有成员以验证内部状态
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from apps.cases.models import Case
from apps.execution.models import Alert, Event, ExecutionLog, Order, SuiteRun
from apps.execution.services import claim_suite_run, create_suite_run
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.watchlists.models import Symbol
from runner.executor import CaseExecutionError
from runner.queue import TaskQueue, WorkerPool
from runner.registry import PlanRegistry
from runner.scheduler import Scheduler, _as_aware
from runner.service import PlanExecutionService, build_execution_service

NOW = datetime(2026, 8, 26, 10, 30)
CRON = '30 10 * * *'


class _RecordingRunner:
    """最小 runner 替身：记录被执行的 (plan_id, symbol)，不触碰数据库。"""

    def __init__(self, fail=False):
        self.executed: list[tuple] = []
        self.fail = fail

    async def arun(self, plan, symbol, payload=None):
        self.executed.append((getattr(plan, 'pk', plan), symbol))
        if self.fail:
            raise RuntimeError('boom')
        return 'ok'


class WorkerPoolRunForeverTest(TestCase):
    """常驻消费语义（纯队列，不触碰数据库）。"""

    def test_run_forever_keeps_running_on_empty_queue_until_stopped(self):
        queue = TaskQueue()

        async def scenario():
            stop = asyncio.Event()
            runner = _RecordingRunner()
            pool = WorkerPool(runner, worker_count=1)
            task = asyncio.create_task(pool.run_forever(queue, stop))
            await asyncio.sleep(0.05)          # 队列为空也应保持运行
            self.assertFalse(task.done())
            queue.put_nowait(_PlanStub(), '000001')
            # task_done() 在 worker 记录结果之后调用，join 返回即代表已处理完
            await asyncio.wait_for(queue.join(), timeout=10)
            stop.set()
            await asyncio.wait_for(task, timeout=5)
            return runner

        runner = asyncio.run(scenario())
        self.assertEqual(runner.executed, [(1, '000001')])

    def test_run_forever_keeps_consuming_after_a_failing_task(self):
        """守护进程语义：单任务失败不中断消费。"""
        queue = TaskQueue()

        async def scenario():
            stop = asyncio.Event()
            runner = _RecordingRunner(fail=True)
            pool = WorkerPool(runner, worker_count=1)
            task = asyncio.create_task(pool.run_forever(queue, stop))
            for symbol in ('000001', '000002', '000003'):
                queue.put_nowait(_PlanStub(), symbol)
            await asyncio.wait_for(queue.join(), timeout=10)
            stop.set()
            await asyncio.wait_for(task, timeout=5)
            return runner

        with self.assertLogs('runner.queue', level='ERROR'):
            runner = asyncio.run(scenario())
        self.assertEqual(
            [symbol for _, symbol in runner.executed],
            ['000001', '000002', '000003'],
        )

    def test_run_still_raises_first_error(self):
        """历史语义不变：``run`` 排空后仍抛出首个错误。"""
        queue = TaskQueue()
        queue.put_nowait(_PlanStub(), '000001')
        with self.assertRaisesRegex(RuntimeError, 'boom'):
            asyncio.run(WorkerPool(_RecordingRunner(fail=True)).run(queue))


class _PlanStub:
    retry_policy: dict = {}
    pk = 1


def _make_due_plan(code='000001', status='published', version=1, cron=CRON):
    """建一个"当前 cron 命中"的已发布时间驱动 Plan（含 1 个订阅标的）。"""
    PlanRegistry._plans.clear()   # 进程级缓存：用例间主键会复用，先清干净
    Symbol.objects.get_or_create(code=code, defaults={'name': '测试标的', 'market': 'A'})
    suite = Suite.objects.create(name=f'Suite {code}', status='published')
    suite.cases.set([Case.objects.create(
        name=f'Case {code}', node_type='executor', status='published',
        params={
            'trigger': {'event_type': 'SUITE_INIT'},
            'result': {'direction': 1,
                       'order': {'direction': 'buy', 'price': 10, 'volume': 10}},
            'symbol_scope': {'type': 'symbols', 'symbol_codes': [code]},
        },
    )])
    return Plan.objects.create(
        name=f'Plan {code}', root_suite=suite, status=status, version=version,
        trigger_type='time', cron_expr=cron,
        exec_mode='serial',
    )


class SchedulerCollectDueTasksTest(TestCase):
    """同步取任务（DB 侧）。"""

    def test_collect_due_tasks_returns_plan_and_symbol(self):
        plan = _make_due_plan()
        tasks = Scheduler().collect_due_tasks(NOW)
        self.assertEqual(tasks, [(plan, '000001')])

    def test_collect_due_tasks_deduplicates_same_minute(self):
        _make_due_plan()
        scheduler = Scheduler()
        self.assertEqual(len(scheduler.collect_due_tasks(NOW)), 1)
        self.assertEqual(scheduler.collect_due_tasks(NOW), [])        # 同分钟不重复
        # 10:31 不命中 '30 10 * * *'
        self.assertEqual(scheduler.collect_due_tasks(NOW + timedelta(minutes=1)), [])

    def test_collect_due_tasks_reenqueues_on_next_matching_minute(self):
        _make_due_plan(cron='* * * * *')           # 每分钟命中
        scheduler = Scheduler()
        self.assertEqual(len(scheduler.collect_due_tasks(NOW)), 1)
        self.assertEqual(scheduler.collect_due_tasks(NOW), [])        # 同分钟去重
        self.assertEqual(len(scheduler.collect_due_tasks(NOW + timedelta(minutes=1))), 1)

    def test_collect_due_tasks_skips_unpublished_plans(self):
        _make_due_plan(status='archived')
        self.assertEqual(Scheduler().collect_due_tasks(NOW), [])

    def test_prune_enqueued_bounds_key_set(self):
        scheduler = Scheduler()
        scheduler._enqueued_limit = 5
        stale = NOW - timedelta(days=1)
        scheduler._enqueued = {
            (i, 1, '000001', stale.year, stale.month, stale.day, 10, 30)
            for i in range(10)
        }
        _make_due_plan()
        scheduler.collect_due_tasks(NOW)
        # 历史键被裁掉，只剩当天（本轮新增）的键
        self.assertTrue(scheduler._enqueued)
        for key in scheduler._enqueued:
            self.assertEqual((key[3], key[4], key[5]), (NOW.year, NOW.month, NOW.day))

    def test_prune_enqueued_is_noop_below_limit(self):
        scheduler = Scheduler()
        scheduler._enqueued_limit = 100
        scheduler._enqueued = {(1, 1, 'x', 2000, 1, 1, 0, 0)}
        scheduler._prune_enqueued(NOW)
        self.assertEqual(len(scheduler._enqueued), 1)


class SchedulerAsyncLoopTest(TransactionTestCase):
    """核心修复：异步轮询把到期任务投进队列，并被 WorkerPool 真正执行。"""

    def test_async_loop_enqueues_and_worker_executes_plan(self):
        plan = _make_due_plan()
        executed: list[tuple] = []

        class RecordingService(PlanExecutionService):
            def run(self, plan_obj, symbol, payload=None):  # pylint: disable=arguments-renamed  # 沿用 plan_obj 以免遮蔽用例里的外层 plan 局部变量
                result = super().run(plan_obj, symbol, payload)
                executed.append((plan_obj.pk, symbol))
                return result

        scheduler = Scheduler(poll_interval=0.01)
        service = RecordingService(risk_controller=None, data_context_builder=None)
        pool = WorkerPool(service, worker_count=1)

        async def scenario():
            stop = asyncio.Event()
            sched = asyncio.create_task(
                scheduler.run_forever_async(stop, clock=lambda: NOW))
            consume = asyncio.create_task(pool.run_forever(scheduler.task_queue, stop))
            for _ in range(200):
                await asyncio.sleep(0.02)
                if executed:
                    break
            stop.set()
            await asyncio.wait_for(asyncio.gather(sched, consume), timeout=5)

        asyncio.run(scenario())

        self.assertEqual(executed, [(plan.pk, '000001')])
        run = SuiteRun.objects.get(plan=plan, symbol='000001')
        self.assertEqual(run.status, 'completed')
        self.assertTrue(ExecutionLog.objects.filter(plan=plan, symbol='000001').exists())
        self.assertTrue(Order.objects.filter(symbol='000001').exists())

    def test_async_loop_stops_promptly_on_stop_event(self):
        scheduler = Scheduler(poll_interval=30)   # 远大于停止等待时间

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(
                scheduler.run_forever_async(stop, clock=lambda: NOW))
            await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, timeout=2)   # 不等满 30s 轮询周期

        asyncio.run(scenario())

    def test_async_loop_honours_scheduler_stop(self):
        scheduler = Scheduler(poll_interval=30)
        scheduler.stop()

        async def scenario():
            await asyncio.wait_for(scheduler.run_forever_async(clock=lambda: NOW), timeout=2)

        asyncio.run(scenario())   # 立即返回（未进入循环）


class PlanExecutionServiceTest(TestCase):
    """生产执行端：计划校验、真实执行、失败告警。"""

    def _service(self, **kwargs):
        kwargs.setdefault('risk_controller', None)
        kwargs.setdefault('data_context_builder', None)
        return PlanExecutionService(**kwargs)

    def test_run_executes_plan_and_persists_log_and_order(self):
        plan = _make_due_plan()
        result = self._service().run(plan, '000001')
        self.assertIsNotNone(result)
        run = SuiteRun.objects.get(plan=plan, symbol='000001')
        self.assertEqual(run.status, 'completed')
        self.assertTrue(ExecutionLog.objects.filter(plan=plan, symbol='000001').exists())
        order = Order.objects.get(symbol='000001')
        self.assertEqual((order.direction, order.volume), ('buy', 10))

    def test_run_skips_unpublished_plan(self):
        plan = _make_due_plan(status='archived')
        self.assertIsNone(self._service().run(plan, '000001'))
        self.assertFalse(SuiteRun.objects.exists())

    def test_run_skips_missing_plan(self):
        self.assertIsNone(self._service().run(999999, '000001'))
        self.assertFalse(SuiteRun.objects.exists())

    def test_run_accepts_plan_pk_and_refetches_fresh_state(self):
        """入队后 Plan 被归档 → 跳过（不回读过期实例状态）。"""
        plan = _make_due_plan()
        Plan.objects.filter(pk=plan.pk).update(status='archived')
        self.assertIsNone(self._service().run(plan.pk, '000001'))
        self.assertFalse(SuiteRun.objects.exists())

    def test_failure_raises_and_emits_suite_failed_alert(self):
        plan = _make_due_plan()

        class BoomRunner:
            def run(self, plan_obj, symbol, payload=None, on_run=None, risk_controller=None):
                run = SuiteRun.objects.create(
                    plan=plan_obj, suite=plan_obj.root_suite, symbol=symbol)
                on_run(run)
                raise CaseExecutionError('boom')

        service = PlanExecutionService(suite_runner=BoomRunner())
        with self.assertRaises(CaseExecutionError):
            service.run(plan, '000001')

        alert = Alert.objects.get(alert_type='suite_failed')
        self.assertEqual(alert.suite_run.symbol, '000001')
        self.assertIn('boom', alert.message)

    def test_alert_failure_does_not_mask_original_error(self):
        """告警本身出错时不能覆盖原始异常。"""
        plan = _make_due_plan()

        class BoomRunner:
            def run(self, plan_obj, symbol, payload=None, on_run=None, risk_controller=None):
                run = SuiteRun.objects.create(
                    plan=plan_obj, suite=plan_obj.root_suite, symbol=symbol)
                on_run(run)
                raise CaseExecutionError('original-boom')

        service = PlanExecutionService(suite_runner=BoomRunner())
        with patch('apps.execution.alerts.alert_service.create_suite_failed_alert',
                   side_effect=RuntimeError('alert down')):
            with self.assertRaisesRegex(CaseExecutionError, 'original-boom'):
                service.run(plan, '000001')

    def test_alert_skipped_when_no_run_handle(self):
        class BoomRunner:
            def run(self, plan_obj, symbol, payload=None, on_run=None, risk_controller=None):
                raise CaseExecutionError('early-boom')

        plan = _make_due_plan()
        service = PlanExecutionService(suite_runner=BoomRunner())
        with self.assertRaises(CaseExecutionError):
            service.run(plan, '000001')
        self.assertFalse(Alert.objects.exists())


class PlanRegistryRefreshTest(TestCase):
    """注册中心自愈：可执行配置被改动（版本未变）也应刷新缓存。"""

    def test_sync_refreshes_when_executable_config_changes_without_version_bump(self):
        plan = _make_due_plan(cron='30 10 * * *')
        PlanRegistry.sync_from_database()
        self.assertEqual(PlanRegistry.get_snapshot(plan.pk)['cron_expr'], '30 10 * * *')

        # 已发布 Plan 直接编辑（update_plan 不改 version）
        plan.cron_expr = '*/5 * * * *'
        plan.save(update_fields=('cron_expr', 'updated_at'))
        PlanRegistry.sync_from_database()
        self.assertEqual(PlanRegistry.get_snapshot(plan.pk)['cron_expr'], '*/5 * * * *')

    def test_case_symbol_change_is_picked_up_without_version_bump(self):
        """标的范围已下沉到 Case：改 Case.params 即刻影响 Plan 的标的集合。

        标的范围不再进 PlanRegistry 快照（``_executable_keys`` 已移除该键），
        解析时直接读 Case，因此"改配置后重新解析"由 Case 侧生效。
        """
        from apps.plans.services import resolve_plan_symbols

        plan = _make_due_plan()
        Symbol.objects.get_or_create(code='600000', defaults={'name': '浦发银行', 'market': 'A'})
        self.assertEqual([s.code for s in resolve_plan_symbols(plan)], ['000001'])

        case = plan.root_suite.cases.get()
        case.params = {**case.params,
                       'symbol_scope': {'type': 'symbols', 'symbol_codes': ['600000']}}
        case.save(update_fields=('params', 'updated_at'))
        self.assertEqual([s.code for s in resolve_plan_symbols(plan)], ['600000'])

    def test_sync_drops_archived_plan(self):
        plan = _make_due_plan()
        PlanRegistry.sync_from_database()
        self.assertIsNotNone(PlanRegistry.get(plan.pk))
        plan.status = 'archived'
        plan.save(update_fields=('status', 'updated_at'))
        PlanRegistry.sync_from_database()
        self.assertIsNone(PlanRegistry.get(plan.pk))


class BuildExecutionServiceTest(TestCase):
    """执行服务装配：下单通道与风控开关。"""

    def test_rejects_unknown_order_broker(self):
        with self.assertRaisesRegex(ValueError, 'order_broker'):
            build_execution_service(order_broker='ib')

    def test_none_broker_disables_order_submission(self):
        service = build_execution_service(order_broker='none')
        self.assertIsNone(service.broker)
        self.assertIsNotNone(service._suite_runner.risk_controller)

    def test_risk_control_can_be_disabled(self):
        service = build_execution_service(order_broker='none', enable_risk=False)
        self.assertIsNone(service._suite_runner.risk_controller)

    def test_gm_broker_is_constructed_when_selected(self):
        sentinel = object()
        with patch('runner.gm_adapter.GmBrokerAdapter', return_value=sentinel) as ctor:
            service = build_execution_service(order_broker='gm')
        ctor.assert_called_once_with()
        self.assertIs(service.broker, sentinel)


class ClaimSuiteRunTest(TestCase):
    """P1：``pending`` 运行的原子认领（CAS）——重复投递只生效一次。"""

    def test_claim_moves_pending_to_running_and_enqueues_start(self):
        plan = _make_due_plan()
        run = create_suite_run(plan, '000001')

        self.assertTrue(claim_suite_run(run))

        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        self.assertIsNotNone(run.started_at)
        self.assertEqual(
            list(Event.objects.filter(run=run).order_by('pk')
                 .values_list('event_type', flat=True)),
            ['SUITE_INIT', 'SUITE_START'],
        )

    def test_second_claim_fails(self):
        plan = _make_due_plan()
        run = create_suite_run(plan, '000001')
        claim_suite_run(run)

        self.assertFalse(claim_suite_run(run))

    def test_claim_fails_for_terminal_run(self):
        plan = _make_due_plan()
        run = create_suite_run(plan, '000001')
        SuiteRun.objects.filter(pk=run.pk).update(status='completed')

        self.assertFalse(claim_suite_run(run))

    def test_find_active_run_prefers_unfinished_run(self):
        from apps.execution.services import find_active_run

        plan = _make_due_plan()
        run = create_suite_run(plan, '000001')
        self.assertEqual(find_active_run(plan, '000001').pk, run.pk)

        SuiteRun.objects.filter(pk=run.pk).update(status='completed')
        self.assertIsNone(find_active_run(plan, '000001'))


class PendingDispatchTest(TestCase):
    """P1：把到期任务落库为持久化执行意向，并补投遗留 pending（不丢、不重复）。"""

    def _pending(self, code, seconds_ago=0):
        plan = _make_due_plan(code=code)
        run = create_suite_run(plan, code)
        SuiteRun.objects.filter(pk=run.pk).update(
            created_at=_as_aware(NOW) - timedelta(seconds=seconds_ago))
        run.refresh_from_db()
        return plan, run

    def test_materialize_creates_pending_intent_with_run_id(self):
        plan = _make_due_plan()

        tasks = Scheduler().materialize_due_tasks(NOW)

        self.assertEqual(len(tasks), 1)
        got_plan, symbol, payload = tasks[0]
        self.assertEqual((got_plan.pk, symbol), (plan.pk, '000001'))
        run = SuiteRun.objects.get(pk=payload['suite_run_id'])
        self.assertEqual((run.status, run.plan_id, run.symbol),
                         ('pending', plan.pk, '000001'))

    def test_materialize_reuses_active_run_without_creating_second(self):
        """重启后同一分钟重复轮询：DB 层去重，不产生第二条运行。"""
        plan = _make_due_plan()
        Scheduler().materialize_due_tasks(NOW)

        second = Scheduler().materialize_due_tasks(NOW)   # 全新实例 = 内存去重/队列皆空

        self.assertEqual(SuiteRun.objects.filter(plan=plan).count(), 1)
        self.assertEqual(second[0][2]['suite_run_id'],
                         SuiteRun.objects.get(plan=plan).pk)

    def test_materialize_skips_expired_pending(self):
        plan = _make_due_plan()
        run = create_suite_run(plan, '000001')
        SuiteRun.objects.filter(pk=run.pk).update(
            created_at=_as_aware(NOW) - timedelta(seconds=600))   # 超过 300s 有效期

        self.assertEqual(Scheduler().materialize_due_tasks(NOW), [])

    def test_collect_pending_sweeps_settled_intent(self):
        _plan, run = self._pending('000003', seconds_ago=30)

        tasks = Scheduler(pending_sweep_min_age=5).collect_pending_tasks(NOW)

        self.assertEqual([item[2]['suite_run_id'] for item in tasks], [run.pk])
        self.assertEqual(tasks[0][1], '000003')

    def test_collect_pending_excludes_too_fresh_and_expired(self):
        self._pending('000004', seconds_ago=1)      # 太新（可能与即时投递竞争）
        self._pending('000005', seconds_ago=600)    # 已过期

        tasks = Scheduler(pending_sweep_min_age=5).collect_pending_tasks(NOW)

        self.assertEqual(tasks, [])

    def test_collect_pending_skips_unpublished_plan(self):
        plan, _run = self._pending('000006', seconds_ago=30)
        Plan.objects.filter(pk=plan.pk).update(status='archived')

        self.assertEqual(
            Scheduler(pending_sweep_min_age=5).collect_pending_tasks(NOW), [])

    def test_due_tasks_deduplicates_by_run(self):
        _plan, run = self._pending('000007', seconds_ago=30)

        tasks = Scheduler(pending_sweep_min_age=5).due_tasks(NOW)

        self.assertEqual([item[2]['suite_run_id'] for item in tasks], [run.pk])

    def test_enqueue_due_plans_carries_run_id_in_payload(self):
        plan = _make_due_plan()

        queue = Scheduler().enqueue_due_plans(NOW)

        got_plan, symbol, payload = queue._queue.get_nowait()
        self.assertEqual((got_plan.pk, symbol), (plan.pk, '000001'))
        self.assertEqual(payload['suite_run_id'],
                         SuiteRun.objects.get(plan=plan).pk)

    def test_sweep_recovers_intent_when_cron_no_longer_matches(self):
        """内存队列丢失 + cron 已不命中：仅靠 pending 补投也能执行。"""
        plan = _make_due_plan(cron='0 3 * * *')          # 与 NOW 不命中
        run = create_suite_run(plan, '000001')
        SuiteRun.objects.filter(pk=run.pk).update(
            created_at=_as_aware(NOW) - timedelta(seconds=30))

        tasks = Scheduler(pending_sweep_min_age=0).due_tasks(NOW)

        self.assertEqual([item[2]['suite_run_id'] for item in tasks], [run.pk])
        service = PlanExecutionService(risk_controller=None, data_context_builder=None)
        log = service.run(tasks[0][0], tasks[0][1], tasks[0][2])
        run.refresh_from_db()
        self.assertIsNotNone(log)
        self.assertEqual(run.status, 'completed')
        self.assertEqual(SuiteRun.objects.filter(plan=plan).count(), 1)


class PlanExecutionServiceClaimTest(TestCase):
    """P1：执行端按持久化意向执行（认领 + 幂等，不重复下单）。"""

    def test_run_claimed_executes_existing_run(self):
        plan = _make_due_plan()
        run = create_suite_run(plan, '000001')
        service = PlanExecutionService(risk_controller=None, data_context_builder=None)

        log = service.run(plan, '000001', {'suite_run_id': run.pk})

        run.refresh_from_db()
        self.assertIsNotNone(log)
        self.assertEqual(run.status, 'completed')
        self.assertEqual(ExecutionLog.objects.filter(plan=plan).count(), 1)
        self.assertEqual(Order.objects.filter(symbol='000001').count(), 1)

    def test_run_claimed_is_idempotent_and_does_not_duplicate_orders(self):
        plan = _make_due_plan()
        run = create_suite_run(plan, '000001')
        service = PlanExecutionService(risk_controller=None, data_context_builder=None)

        first = service.run(plan, '000001', {'suite_run_id': run.pk})
        second = service.run(plan, '000001', {'suite_run_id': run.pk})

        self.assertIsNotNone(first)
        self.assertIsNone(second)                     # 已终态 → 认领失败 → 跳过
        self.assertEqual(SuiteRun.objects.filter(plan=plan).count(), 1)
        self.assertEqual(ExecutionLog.objects.filter(plan=plan).count(), 1)
        self.assertEqual(Order.objects.filter(symbol='000001').count(), 1)

    def test_run_claimed_skips_unknown_run(self):
        plan = _make_due_plan()
        service = PlanExecutionService(risk_controller=None, data_context_builder=None)

        self.assertIsNone(service.run(plan, '000001', {'suite_run_id': 999999}))
        self.assertEqual(SuiteRun.objects.count(), 0)

    def test_legacy_payload_still_creates_a_new_run(self):
        """兼容路径不变：无 ``suite_run_id`` 时按老语义新建运行。"""
        plan = _make_due_plan()
        service = PlanExecutionService(risk_controller=None, data_context_builder=None)

        log = service.run(plan, '000001')

        self.assertIsNotNone(log)
        self.assertEqual(SuiteRun.objects.filter(plan=plan).count(), 1)
        self.assertEqual(SuiteRun.objects.get(plan=plan).status, 'completed')


class RestartDurabilityTest(TransactionTestCase):
    """P1：进程重启不丢执行意向、不重复执行（跨线程，故用 TransactionTestCase）。"""

    def test_pending_intent_survives_restart_and_runs_exactly_once(self):
        plan = _make_due_plan()
        Scheduler().enqueue_due_plans(NOW)               # 第一个进程：落库意向 + 投递
        run = SuiteRun.objects.get(plan=plan, symbol='000001')
        SuiteRun.objects.filter(pk=run.pk).update(
            created_at=_as_aware(NOW) - timedelta(seconds=30))
        run.refresh_from_db()
        self.assertEqual(run.status, 'pending')

        executed = []

        class RecordingService(PlanExecutionService):
            def run(self, plan_obj, symbol, payload=None):  # pylint: disable=arguments-renamed  # 沿用 plan_obj 以免遮蔽用例里的外层 plan 局部变量
                result = super().run(plan_obj, symbol, payload)
                executed.append((plan_obj.pk, symbol))
                return result

        # 模拟重启：全新 Scheduler（内存去重/队列皆空）+ 新执行服务
        scheduler = Scheduler(poll_interval=0.01, pending_sweep_min_age=0)
        service = RecordingService(risk_controller=None, data_context_builder=None)
        pool = WorkerPool(service, worker_count=1)

        async def scenario():
            stop = asyncio.Event()
            sched = asyncio.create_task(
                scheduler.run_forever_async(stop, clock=lambda: NOW))
            consume = asyncio.create_task(pool.run_forever(scheduler.task_queue, stop))
            for _ in range(200):
                await asyncio.sleep(0.02)
                if executed:
                    break
            stop.set()
            await asyncio.wait_for(asyncio.gather(sched, consume), timeout=5)

        asyncio.run(scenario())

        runs = SuiteRun.objects.filter(plan=plan, symbol='000001')
        self.assertEqual(runs.count(), 1)                # 未产生第二条运行
        self.assertEqual(executed, [(plan.pk, '000001')])
        self.assertEqual(runs.get().status, 'completed')
