"""执行日志生命周期管理（N-04）测试：清理范围、订单保护、幂等、dry-run 与调度门禁。"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from apps.execution.models import Event, ExecutionLog, NodeRun, Order, SuiteRun
from apps.execution.retention import RetentionError, purge_execution_history
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.users.models import User
from apps.watchlists.models import Symbol


class ExecutionRetentionTest(TestCase):
    """N-04：执行日志保留 30 天；清理不影响未完成运行和订单。"""

    def setUp(self):
        self.user = User.objects.create_user(username='retention', password='test')
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.suite = Suite.objects.create(name='retention-suite', created_by=self.user)
        self.plan = Plan.objects.create(
            name='retention-plan', root_suite=self.suite,
            trigger_type='manual', created_by=self.user,
        )
        self.now = timezone.now()

    # ---- 构造工具 -------------------------------------------------
    def _make_run(self, status='completed', days_old=40):
        run = SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001',
            status=status, event_queue=[],
        )
        stamp = self.now - timedelta(days=days_old)
        fields = {'created_at': stamp}
        if status in ('completed', 'failed', 'stopped'):
            fields['ended_at'] = stamp
        SuiteRun.objects.filter(pk=run.pk).update(**fields)
        return run

    def _make_event(self, run, days_old=40):
        event = Event.objects.create(run=run, event_type='SUITE_INIT', source='test', status='done')
        Event.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=days_old))
        return event

    def _make_node_run(self, run):
        return NodeRun.objects.create(run=run, node_type='suite', suite=self.suite, status='completed')

    def _make_log(self, days_old=40, with_order=False):
        log = ExecutionLog.objects.create(
            plan=self.plan, symbol='000001', final_direction=1, status='success',
        )
        ExecutionLog.objects.filter(pk=log.pk).update(
            trigger_time=self.now - timedelta(days=days_old),
        )
        if with_order:
            Order.objects.create(
                log=log, symbol='000001', direction='buy',
                price=Decimal('10.5000'), volume=100, status='filled',
            )
        return log

    # ---- 用例 ------------------------------------------------------
    def test_purges_stale_terminal_run_traces_and_orphan_logs(self):
        """过期终态运行的事件/轨迹与无订单日志被清理。"""
        run = self._make_run(status='completed', days_old=40)
        event = self._make_event(run)
        node_run = self._make_node_run(run)
        log = self._make_log(days_old=40)

        stats = purge_execution_history(now=self.now)

        self.assertFalse(Event.objects.filter(pk=event.pk).exists())
        self.assertFalse(NodeRun.objects.filter(pk=node_run.pk).exists())
        self.assertFalse(ExecutionLog.objects.filter(pk=log.pk).exists())
        self.assertEqual(stats['runs_scanned'], 1)
        self.assertEqual(stats['events'], 1)
        self.assertEqual(stats['node_runs'], 1)
        self.assertEqual(stats['logs'], 1)

    def test_keeps_recent_and_unfinished_runs(self):
        """保留期内的运行与未完成（pending/running）运行一律不动。"""
        recent_run = self._make_run(status='completed', days_old=1)
        recent_event = self._make_event(recent_run, days_old=1)
        running_run = self._make_run(status='running', days_old=40)
        running_event = self._make_event(running_run)
        pending_run = self._make_run(status='pending', days_old=40)
        pending_event = self._make_event(pending_run)

        purge_execution_history(now=self.now)

        self.assertTrue(Event.objects.filter(pk=recent_event.pk).exists())
        self.assertTrue(Event.objects.filter(pk=running_event.pk).exists())
        self.assertTrue(Event.objects.filter(pk=pending_event.pk).exists())
        self.assertTrue(SuiteRun.objects.filter(pk=running_run.pk).exists())
        self.assertTrue(SuiteRun.objects.filter(pk=pending_run.pk).exists())

    def test_keeps_logs_with_orders_and_orders_survive(self):
        """含订单的执行日志必须保留（Order.log 为 CASCADE），订单本体不受影响。"""
        self._make_run(status='completed', days_old=40)
        log = self._make_log(days_old=40, with_order=True)

        stats = purge_execution_history(now=self.now)

        self.assertTrue(ExecutionLog.objects.filter(pk=log.pk).exists())
        self.assertEqual(Order.objects.filter(log=log).count(), 1)
        self.assertEqual(stats['logs'], 0)
        self.assertEqual(stats['logs_kept_with_orders'], 1)

    def test_dry_run_reports_without_deleting(self):
        run = self._make_run(status='completed', days_old=40)
        event = self._make_event(run)
        log = self._make_log(days_old=40)

        stats = purge_execution_history(now=self.now, dry_run=True)

        self.assertTrue(stats['dry_run'])
        self.assertEqual(stats['events'], 1)
        self.assertEqual(stats['logs'], 1)
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())
        self.assertTrue(ExecutionLog.objects.filter(pk=log.pk).exists())

    def test_idempotent_repeat_run_deletes_zero(self):
        """清理任务可重复执行且幂等。"""
        run = self._make_run(status='completed', days_old=40)
        self._make_event(run)
        self._make_log(days_old=40)

        first = purge_execution_history(now=self.now)
        second = purge_execution_history(now=self.now)

        self.assertEqual(first['events'], 1)
        self.assertEqual(second['events'], 0)
        self.assertEqual(second['logs'], 0)
        self.assertEqual(second['node_runs'], 0)

    def test_custom_days_and_invalid_days(self):
        run = self._make_run(status='completed', days_old=10)
        event = self._make_event(run, days_old=10)

        # 保留 5 天 → 10 天前的记录被清理
        purge_execution_history(days=5, now=self.now)
        self.assertFalse(Event.objects.filter(pk=event.pk).exists())
        with self.assertRaises(RetentionError):
            purge_execution_history(days=-1, now=self.now)


class SchedulerRetentionGateTest(TestCase):
    """N-04：Scheduler 每日至多清理一次，受开关控制，失败可重试。"""

    def setUp(self):
        from runner.scheduler import Scheduler

        self.scheduler = Scheduler()
        self.now = timezone.now()

    def test_daily_purge_runs_once(self):
        with mock.patch(
            'apps.execution.retention.purge_execution_history',
            return_value={'events': 0},
        ) as purge:
            self.assertIsNotNone(self.scheduler._maybe_purge_logs(self.now))
            # 同一自然日重复调用不执行
            self.assertIsNone(self.scheduler._maybe_purge_logs(self.now))
            self.assertEqual(purge.call_count, 1)
            # 次日再次执行
            self.assertIsNotNone(self.scheduler._maybe_purge_logs(self.now + timedelta(days=1)))
            self.assertEqual(purge.call_count, 2)

    def test_disabled_switch_skips_purge(self):
        with mock.patch.object(settings, 'EXECUTION_LOG_RETENTION_ENABLED', False), \
                mock.patch('apps.execution.retention.purge_execution_history') as purge:
            self.assertIsNone(self.scheduler._maybe_purge_logs(self.now))
            purge.assert_not_called()

    def test_failure_does_not_mark_completed(self):
        with mock.patch(
            'apps.execution.retention.purge_execution_history',
            side_effect=RuntimeError('db busy'),
        ):
            self.assertIsNone(self.scheduler._maybe_purge_logs(self.now))
        self.assertIsNone(self.scheduler._last_purge_date)