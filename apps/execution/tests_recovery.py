"""P0 重启恢复测试：非正常终止遗留运行的收口、幂等、dry-run 与告警。"""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.execution.models import Alert, Event, ExecutionLog, NodeRun, SuiteRun
from apps.execution.recovery import ORPHAN_ERROR_CODE, recover_orphaned_runs
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.users.models import User
from apps.watchlists.models import Symbol


class OrphanRecoveryTest(TestCase):
    """P0：进程重启后收口上次遗留的 ``running`` 运行（只收口，不自动续跑）。"""

    def setUp(self):
        self.user = User.objects.create_user(username='recovery', password='test')
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.suite = Suite.objects.create(name='recovery-suite', created_by=self.user)
        self.plan = Plan.objects.create(
            name='recovery-plan', root_suite=self.suite,
            trigger_type='manual', created_by=self.user,
        )
        self.now = timezone.now()

    # ---- 构造工具 -------------------------------------------------
    def _make_run(self, status='running'):
        started = None if status == 'pending' else self.now - timedelta(minutes=5)
        return SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001',
            status=status, event_queue=[], started_at=started,
        )

    def _make_event(self, run, status='processing'):
        event = Event.objects.create(
            run=run, event_type='SUITE_INIT', source='test', status=status)
        SuiteRun.objects.filter(pk=run.pk).update(event_queue=[event.pk])
        run.refresh_from_db()
        return event

    # ---- 用例 -----------------------------------------------------
    def test_running_run_is_closed_with_trace_and_alert(self):
        run = self._make_run()
        event = self._make_event(run, status='processing')
        node = NodeRun.objects.create(
            run=run, node_type='suite', suite=self.suite, status='running')

        stats = recover_orphaned_runs(now=self.now)

        run.refresh_from_db()
        event.refresh_from_db()
        node.refresh_from_db()
        log = ExecutionLog.objects.get(task_id=f'suite-run-{run.pk}')

        self.assertEqual(stats['runs'], 1)
        self.assertEqual(stats['run_ids'], [run.pk])
        self.assertEqual(stats['events_reset'], 1)
        self.assertEqual(stats['node_runs_failed'], 1)
        self.assertEqual(stats['logs'], 1)
        self.assertEqual(stats['alerts'], 1)

        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.ended_at, self.now)
        self.assertEqual(event.status, 'pending')       # 在途事件回退，痕迹保留
        self.assertIsNone(event.processed_at)
        self.assertEqual(node.status, 'failed')
        self.assertEqual(node.ended_at, self.now)
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.error_code, ORPHAN_ERROR_CODE)
        self.assertTrue(Alert.objects.filter(
            suite_run=run, error_code=ORPHAN_ERROR_CODE).exists())

    def test_idempotent_second_run_makes_no_change(self):
        run = self._make_run()
        self._make_event(run, status='processing')
        recover_orphaned_runs(now=self.now)
        logs = ExecutionLog.objects.count()
        alerts = Alert.objects.count()

        stats = recover_orphaned_runs(now=self.now + timedelta(minutes=1))

        self.assertEqual(stats['runs'], 0)
        self.assertEqual(stats['run_ids'], [])
        self.assertEqual(ExecutionLog.objects.count(), logs)
        self.assertEqual(Alert.objects.count(), alerts)

    def test_pending_run_is_reported_but_not_closed(self):
        pending = self._make_run(status='pending')

        stats = recover_orphaned_runs(now=self.now)

        pending.refresh_from_db()
        self.assertEqual(pending.status, 'pending')     # 执行意向不默认收口
        self.assertEqual(stats['runs'], 0)
        self.assertEqual(stats['pending_runs'], 1)

    def test_dry_run_does_not_write(self):
        run = self._make_run()
        self._make_event(run, status='processing')
        node = NodeRun.objects.create(
            run=run, node_type='suite', suite=self.suite, status='running')

        stats = recover_orphaned_runs(now=self.now, dry_run=True)

        run.refresh_from_db()
        node.refresh_from_db()
        self.assertEqual(stats['runs'], 1)
        self.assertEqual(stats['events_reset'], 0)
        self.assertEqual(run.status, 'running')
        self.assertEqual(node.status, 'running')
        self.assertFalse(ExecutionLog.objects.filter(task_id=f'suite-run-{run.pk}').exists())

    def test_run_ids_scopes_recovery(self):
        keep = self._make_run()
        target = self._make_run()

        stats = recover_orphaned_runs(run_ids=[target.pk], now=self.now)

        keep.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(stats['run_ids'], [target.pk])
        self.assertEqual(keep.status, 'running')
        self.assertEqual(target.status, 'failed')

    def test_existing_log_keeps_trace_fields(self):
        run = self._make_run()
        log = ExecutionLog.objects.create(
            plan=self.plan, symbol='000001', task_id=f'suite-run-{run.pk}',
            final_direction=1, node_snapshots={'7': {'direction': 1}},
            status='success',
        )

        stats = recover_orphaned_runs(now=self.now)

        log.refresh_from_db()
        self.assertEqual(stats['logs'], 1)
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.error_code, ORPHAN_ERROR_CODE)
        self.assertEqual(log.final_direction, 1)                  # 轨迹字段不覆盖
        self.assertEqual(log.node_snapshots, {'7': {'direction': 1}})

    def test_terminal_runs_untouched(self):
        done = SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001', status='completed',
            event_queue=[], started_at=self.now, ended_at=self.now)

        stats = recover_orphaned_runs(now=self.now)

        done.refresh_from_db()
        self.assertEqual(stats['runs'], 0)
        self.assertEqual(done.status, 'completed')

    def test_notify_false_skips_alert(self):
        run = self._make_run()

        stats = recover_orphaned_runs(now=self.now, notify=False)

        run.refresh_from_db()
        self.assertEqual(stats['alerts'], 0)
        self.assertEqual(run.status, 'failed')
        self.assertFalse(Alert.objects.filter(suite_run=run).exists())


class SchedulerStartupRecoveryTest(TestCase):
    """P0：``run_scheduler`` 启动期收口的接线、开关与失败兜底。"""

    def setUp(self):
        self.user = User.objects.create_user(username='startup', password='test')
        self.suite = Suite.objects.create(name='startup-suite', created_by=self.user)
        self.plan = Plan.objects.create(
            name='startup-plan', root_suite=self.suite,
            trigger_type='manual', created_by=self.user,
        )
        self.run = SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001',
            status='running', event_queue=[], started_at=timezone.now(),
        )

    def _command(self):
        from apps.plans.management.commands.run_scheduler import Command

        self.stdout = StringIO()
        self.stderr = StringIO()
        return Command(stdout=self.stdout, stderr=self.stderr)

    def test_startup_closes_orphans_and_reports(self):
        stats = self._command()._recover_orphans()

        self.run.refresh_from_db()
        self.assertEqual(stats['runs'], 1)
        self.assertEqual(self.run.status, 'failed')
        self.assertIn('已收口 1 个遗留未完成运行', self.stdout.getvalue())

    def test_disabled_by_setting(self):
        with self.settings(EXECUTION_ORPHAN_RECOVERY_ENABLED=False):
            stats = self._command()._recover_orphans()

        self.run.refresh_from_db()
        self.assertIsNone(stats)
        self.assertEqual(self.run.status, 'running')
        self.assertIn('跳过遗留运行收口', self.stdout.getvalue())

    def test_recovery_failure_does_not_raise(self):
        with mock.patch('apps.execution.recovery.recover_orphaned_runs',
                        side_effect=RuntimeError('boom')):
            stats = self._command()._recover_orphans()

        self.run.refresh_from_db()
        self.assertIsNone(stats)
        self.assertEqual(self.run.status, 'running')
        self.assertIn('不阻断调度', self.stderr.getvalue())

    def test_no_orphans_message(self):
        SuiteRun.objects.update(status='completed')
        stats = self._command()._recover_orphans()

        self.assertEqual(stats['runs'], 0)
        self.assertIn('无遗留未完成运行', self.stdout.getvalue())
