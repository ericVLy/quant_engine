from datetime import datetime
from decimal import Decimal

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.models import ExecutionLog, Order
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.watchlists.models import Symbol

from .engine import SuiteRunner
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