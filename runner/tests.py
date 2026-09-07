from datetime import datetime
from decimal import Decimal
import asyncio
from threading import Event
from unittest.mock import patch

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.models import ExecutionLog, Order
from apps.plans.models import Plan
from apps.suites.models import Edge, Suite
from apps.suites.services import aggregate_directions
from apps.watchlists.models import Symbol

from .engine import SuiteRunner
from .gm_adapter import GmBrokerAdapter


class _GmStubAPI(object):
    """测试桩：仅提供无副作用的 set_token，屏蔽真实 GM_TOKEN 配置。"""

    def set_token(self, token):
        return None
from .risk import RiskController
from .scheduler import Scheduler
from .registry import PlanRegistry
from .queue import TaskQueue, WorkerPool


class RunnerIntegrationTest(TestCase):
    def setUp(self):
        self.suite = Suite.objects.create(name='Runner Suite', status='published')
        self.plan = Plan.objects.create(
            name='Runner Plan', root_suite=self.suite, status='published',
            symbol_scope={'type': 'symbols'},
        )

    def test_suite_runner_executes_case_and_persists_log_and_order(self):
        case = Case.objects.create(
            name='Buy signal', node_type='executor', status='published',
            params={
                'trigger': {'event_type': 'SUITE_INIT'},
                'result': {
                    'direction': 1,
                    'payload': {'score': 0.9},
                    'order': {'direction': 'buy', 'price': '12.34', 'volume': 100},
                },
            },
        )
        self.suite.cases.add(case)

        log = SuiteRunner().run(self.plan, '000001')

        self.assertEqual(log.status, 'success')
        self.assertEqual(log.final_direction, 1)
        self.assertEqual(log.node_snapshots[str(case.pk)]['score'], 0.9)
        order = Order.objects.get(log=log)
        self.assertEqual(order.symbol, '000001')
        self.assertEqual(order.price, Decimal('12.3400'))
        self.assertEqual(order.volume, 100)

    def test_case_failure_marks_run_failed_and_writes_error_log(self):
        case = Case.objects.create(
            name='Invalid result', node_type='signal', status='published',
            params={'trigger': {'event_type': 'SUITE_INIT'}, 'result': 'invalid'},
        )
        self.suite.cases.add(case)

        with self.assertRaises(Exception):
            SuiteRunner().run(self.plan, '000001')

        self.assertEqual(ExecutionLog.objects.get(symbol='000001').status, 'failed')
        self.assertIn('JSON 对象', ExecutionLog.objects.get(symbol='000001').error_msg)

    def test_risk_block_prevents_broker_submission(self):
        case = Case.objects.create(
            name='Risky order', node_type='executor', status='published',
            params={
                'trigger': {'event_type': 'SUITE_INIT'},
                'result': {'direction': 1, 'order': {
                    'direction': 'buy', 'price': '12.34', 'volume': 100,
                }},
            },
        )
        self.suite.cases.add(case)
        broker = type('Broker', (), {'submit_order': lambda *_args: self.fail('不应下单')})()

        log = SuiteRunner(
            broker=broker, risk_controller=RiskController(max_volume=10)
        ).run(self.plan, '000001')

        self.assertEqual(log.status, 'blocked')
        self.assertEqual(Order.objects.get(log=log).status, 'pending')

    def test_broker_failure_updates_existing_log_and_order(self):
        case = Case.objects.create(
            name='Broker failure', node_type='executor', status='published',
            params={'trigger': {'event_type': 'SUITE_INIT'}, 'result': {
                'direction': 1, 'order': {'direction': 'buy', 'price': 10, 'volume': 1},
            }},
        )
        self.suite.cases.add(case)

        class Broker:
            def submit_order(self, *_args):
                raise RuntimeError('模拟账户拒单')

        with self.assertRaisesRegex(RuntimeError, '模拟账户拒单'):
            SuiteRunner(broker=Broker()).run(self.plan, '000001')
        log = ExecutionLog.objects.get(symbol='000001')
        order = Order.objects.get(log=log)
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.error_code, 'EXECUTION_FAILED')
        self.assertEqual(order.status, 'rejected')
        self.assertEqual(order.last_error, '模拟账户拒单')

    def test_account_level_risk_blocks_insufficient_cash(self):
        class AccountProvider:
            def get_account(self):
                return {'available': 50}

            def get_positions(self):
                return []

        controller = RiskController(
            max_account_value=1000, account_provider=AccountProvider(),
            allowed_sessions=[(0, 0, 23, 59)],
        )
        with patch('runner.risk.timezone.localtime', return_value=datetime(2026, 9, 7, 10)):
            decision = controller.check({'direction': 'buy', 'price': 10, 'volume': 6})
        self.assertFalse(decision.allowed)
        self.assertIn('可用资金不足', decision.reason)

    def test_account_level_risk_blocks_position_limit(self):
        class AccountProvider:
            def get_account(self):
                return {'available': 100000}

            def get_positions(self):
                return [{'market_value': 900, 'volume': 90}]

        controller = RiskController(
            max_position_value=1000, max_position_volume=100,
            account_provider=AccountProvider(), allowed_sessions=[(0, 0, 23, 59)],
        )
        with patch('runner.risk.timezone.localtime', return_value=datetime(2026, 9, 7, 10)):
            decision = controller.check({'direction': 'buy', 'price': 10, 'volume': 20})
        self.assertFalse(decision.allowed)
        self.assertIn('总仓位金额', decision.reason)


class SchedulerTest(TestCase):
    def setUp(self):
        PlanRegistry._plans = {}

    def test_matches_cron_and_enqueues_symbols(self):
        suite = Suite.objects.create(name='Scheduled Suite', status='published')
        Symbol.objects.create(code='000001', name='测试标的', market='A')
        Plan.objects.create(
            name='Scheduled Plan', root_suite=suite, status='published',
            trigger_type='time', cron_expr='30 10 * * *',
            symbol_scope={'type': 'symbols', 'symbol_codes': ['000001']},
        )

        scheduler = Scheduler()
        queue = scheduler.enqueue_due_plans(datetime(2026, 8, 26, 10, 30))

        self.assertFalse(queue.empty())
        self.assertEqual(queue._queue.get_nowait()[1], '000001')

    def test_poll_once_deduplicates_same_minute(self):
        suite = Suite.objects.create(name='去重 Suite', status='published')
        Symbol.objects.create(code='000002', name='测试标的2', market='A')
        Plan.objects.create(
            name='去重 Plan', root_suite=suite, status='published',
            trigger_type='time', cron_expr='30 10 * * *',
            symbol_scope={'type': 'symbols', 'symbol_codes': ['000002']},
        )

        scheduler = Scheduler()
        now = datetime(2026, 8, 26, 10, 30)
        scheduler.poll_once(now)
        scheduler.poll_once(now)

        self.assertEqual(scheduler.task_queue._queue.qsize(), 1)

    def test_poll_once_refreshes_changed_and_removes_archived_plans(self):
        suite = Suite.objects.create(name='刷新 Suite', status='published')
        plan = Plan.objects.create(
            name='刷新 Plan', root_suite=suite, status='published',
            trigger_type='time', cron_expr='30 10 * * *',
            symbol_scope={'type': 'symbols', 'symbol_codes': []},
        )
        scheduler = Scheduler()
        scheduler.poll_once(datetime(2026, 8, 26, 10, 30))
        self.assertEqual(PlanRegistry.get(plan.pk)['version'], 1)

        plan.version = 2
        plan.save(update_fields=('version', 'updated_at'))
        scheduler.poll_once(datetime(2026, 8, 26, 10, 31))
        self.assertEqual(PlanRegistry.get(plan.pk)['version'], 2)

        plan.status = 'archived'
        plan.save(update_fields=('status', 'updated_at'))
        scheduler.poll_once(datetime(2026, 8, 26, 10, 32))
        self.assertIsNone(PlanRegistry.get(plan.pk))

    def test_run_forever_can_be_stopped(self):
        stop_event = Event()
        calls = []

        class Clock:
            def __call__(self):
                calls.append(True)
                stop_event.set()
                return datetime(2026, 8, 26, 10, 30)

        Scheduler(poll_interval=1).run_forever(
            stop_event=stop_event,
            clock=Clock(),
        )
        self.assertEqual(len(calls), 1)

    def test_sunday_cron_matches_zero_and_seven(self):
        sunday = datetime(2026, 8, 30, 10, 0)
        self.assertTrue(Scheduler._matches_cron('0 10 * * 0', sunday))
        self.assertTrue(Scheduler._matches_cron('0 10 * * 7', sunday))


class WorkerPoolTest(TestCase):
    def test_failed_task_is_reported_after_retries(self):
        calls = []

        class PlanStub:
            retry_policy = {'max_retries': 1, 'delay_seconds': 0}

        class RunnerStub:
            async def arun(self, plan, symbol, payload):
                calls.append(symbol)
                raise RuntimeError('task failed')

        queue = TaskQueue()
        queue.put_nowait(PlanStub(), '000001')
        with self.assertRaisesRegex(RuntimeError, 'task failed'):
            asyncio.run(WorkerPool(RunnerStub()).run(queue))
        self.assertEqual(calls, ['000001', '000001'])


class SuiteRuntimeTest(TestCase):
    def test_aggregate_directions(self):
        suite = Suite.objects.create(name='聚合', aggregate_method='vote')
        self.assertEqual(aggregate_directions(suite, [
            {'direction': 1}, {'direction': 1}, {'direction': -1},
        ]), 1)

    def test_runner_routes_to_downstream_suite(self):
        root = Suite.objects.create(name='根', status='published')
        downstream = Suite.objects.create(name='下游', status='published', parent=root)
        Edge.objects.create(
            from_suite=root, to_suite=downstream,
            event_condition={'event_type': 'CASE_COMPLETED'},
        )
        plan = Plan.objects.create(name='路由计划', root_suite=root, status='published')
        case = Case.objects.create(
            name='根节点', node_type='signal', status='published',
            params={'trigger': {'event_type': 'SUITE_INIT'}, 'result': {'direction': 1}},
        )
        downstream_case = Case.objects.create(
            name='下游节点', node_type='signal', status='published',
            params={'trigger': {'event_type': 'CASE_START'}, 'result': {'direction': -1}},
        )
        root.cases.add(case)
        downstream.cases.add(downstream_case)

        log = SuiteRunner().run(plan, '000003')

        # 新编排语义：final_direction 为根节点聚合（root case +1 与下游分支 -1 汇合抵消）
        self.assertEqual(log.final_direction, 0)


class GmOrderReportTest(TestCase):
    def test_report_updates_order_by_external_id(self):
        suite = Suite.objects.create(name='Report Suite')
        plan = Plan.objects.create(name='Report Plan', root_suite=suite)
        log = ExecutionLog.objects.create(symbol='000001', final_direction=1)
        order = Order.objects.create(
            log=log, symbol='000001', direction='buy', price='12.0000',
            volume=100, external_order_id='gm-123',
        )

        adapter = GmBrokerAdapter(api=_GmStubAPI())
        updated = adapter.on_order_status({
            'cl_ord_id': 'gm-123', 'symbol': '000001', 'status': 3, 'price': 12.5,
        })

        order.refresh_from_db()
        self.assertEqual(updated.pk, order.pk)
        self.assertEqual(order.status, 'filled')
        self.assertEqual(order.price, Decimal('12.5000'))

    def test_old_report_cannot_regress_filled_order(self):
        suite = Suite.objects.create(name='Idempotent Report Suite')
        log = ExecutionLog.objects.create(symbol='000002', final_direction=1)
        order = Order.objects.create(
            log=log, symbol='000002', direction='buy', price='12.0000',
            volume=100, filled_volume=100, external_order_id='gm-456', status='filled',
        )
        adapter = GmBrokerAdapter(api=_GmStubAPI())
        adapter.on_order_status({
            'cl_ord_id': 'gm-456', 'symbol': '000002', 'status': 1,
            'price': 12.1, 'filled_volume': 20,
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'filled')
        self.assertEqual(order.filled_volume, 100)


class GmOrderLifecycleTest(TestCase):
    """P1 真实交易回报与订单生命周期联调 —— 模拟回报全链路。

    覆盖：受理(→sent) → 部分成交(→累计 filled_volume) → 完全成交(→filled)，
    以及拒单、撤单、以及重复回报幂等去重。
    """

    def _make_order(self, symbol='000003', external_id='gm-LC-001', volume=200,
                    status='pending'):
        suite = Suite.objects.create(name='LC Suite')
        log = ExecutionLog.objects.create(symbol=symbol, final_direction=1)
        return Order.objects.create(
            log=log, symbol=symbol, direction='buy', price='12.0000',
            volume=volume, external_order_id=external_id, status=status,
        ), GmBrokerAdapter(api=_GmStubAPI())

    def test_partial_fills_accumulate_to_full_fill(self):
        order, adapter = self._make_order()
        # 受理 → sent
        adapter.on_order_status({'cl_ord_id': 'gm-LC-001', 'status': 'accepted'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'sent')
        # 部分成交 60/200
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-001', 'status': 'partial_filled',
            'filled_volume': 60, 'price': 12.3,
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'sent')
        self.assertEqual(order.filled_volume, 60)
        # 部分成交累加到 120/200（仍在 sent）
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-001', 'status': 'partial_filled',
            'filled_volume': 120, 'price': 12.4,
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'sent')
        self.assertEqual(order.filled_volume, 120)
        # 最后一笔部分成交达到 200/200 → 自动推进为 filled
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-001', 'status': 'partial_filled',
            'filled_volume': 200, 'price': 12.5,
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'filled')
        self.assertEqual(order.filled_volume, 200)

    def test_rejection_sets_status_and_records_error_context(self):
        order, adapter = self._make_order(external_id='gm-LC-REJ')
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-REJ', 'status': 'rejected', 'price': 12.0,
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'rejected')

    def test_cancel_report_marks_order_canceled(self):
        order, adapter = self._make_order(external_id='gm-LC-CXL')
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-CXL', 'status': 'canceled', 'price': 12.0,
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'canceled')

    def test_duplicate_report_is_idempotent(self):
        order, adapter = self._make_order()
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-001', 'status': 'filled',
            'filled_volume': 200, 'price': 12.5,
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'filled')
        self.assertEqual(order.filled_volume, 200)
        # 相同指纹的重复回报 → 标记 duplicate，不产生状态/成交量回退
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-001', 'status': 'filled',
            'filled_volume': 200, 'price': 12.5, 'exec_id': 'dup-echo',
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'filled')
        self.assertEqual(order.filled_volume, 200)
        self.assertTrue(order.report_payload['duplicate'])
        self.assertIn(order.report_payload['fingerprint'],
                      order.processed_report_keys)

    def test_progressed_report_is_not_mistaken_for_duplicate(self):
        """不同累计成交量/状态/价格的回报必须被继续处理，不能误判为重复。"""
        order, adapter = self._make_order()
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-001', 'status': 'accepted',
        })
        order.refresh_from_db()
        self.assertEqual(order.status, 'sent')
        adapter.on_order_status({
            'cl_ord_id': 'gm-LC-001', 'status': 'partial_filled',
            'filled_volume': 90, 'price': 12.3,
        })
        order.refresh_from_db()
        self.assertFalse(order.report_payload['duplicate'])
        self.assertEqual(order.filled_volume, 90)
        self.assertEqual(order.status, 'sent')