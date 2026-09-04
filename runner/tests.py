from datetime import datetime
from decimal import Decimal

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.models import ExecutionLog, Order
from apps.plans.models import Plan
from apps.suites.models import Edge, Suite
from apps.suites.services import aggregate_directions
from apps.watchlists.models import Symbol

from .engine import SuiteRunner
from .gm_adapter import GmBrokerAdapter
from .risk import RiskController
from .scheduler import Scheduler


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


class SchedulerTest(TestCase):
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

        adapter = GmBrokerAdapter(api=object())
        updated = adapter.on_order_status({
            'cl_ord_id': 'gm-123', 'symbol': '000001', 'status': 3, 'price': 12.5,
        })

        order.refresh_from_db()
        self.assertEqual(updated.pk, order.pk)
        self.assertEqual(order.status, 'filled')
        self.assertEqual(order.price, Decimal('12.5000'))