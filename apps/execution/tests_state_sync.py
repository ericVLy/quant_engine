"""P2 状态归一测试：``Plan.run_status`` 与执行事实（``SuiteRun``）对齐。

背景：``run_status`` 原本只由 REST 手动 ``/start`` ``/stop`` 流转，自动收口链条
（``complete_case → _try_complete_suite → _try_complete_plan``）在生产链路零调用者，
导致 Plan 长期停在 ``running``。本模块按 ``SuiteRun`` 事实做数据驱动归一。
"""
from io import StringIO
from unittest import mock

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.models import SuiteRun
from apps.execution.state_machine import StateMachineError, start_plan
from apps.execution.state_sync import (
    plan_run_status_from_runs, reconcile_all_plan_run_statuses, reconcile_plan_run_status,
)
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.users.models import User
from apps.watchlists.models import Symbol


def _make_plan(run_status='new', code='000001', with_order=True):
    """建一个已发布的时间驱动 Plan（Case 声明标的，可选带一笔订单）。"""
    user, _ = User.objects.get_or_create(username=f'sync-{code}')
    Symbol.objects.get_or_create(code=code, defaults={'name': '测试标的', 'market': 'A'})
    suite = Suite.objects.create(name=f'sync-suite-{code}', status='published',
                                 created_by=user)
    params = {
        'trigger': {'event_type': 'SUITE_INIT'},
        'symbol_scope': {'type': 'symbols', 'symbol_codes': [code]},
    }
    if with_order:
        params['result'] = {'direction': 1,
                            'order': {'direction': 'buy', 'price': 10, 'volume': 10}}
    suite.cases.set([Case.objects.create(
        name=f'sync-case-{code}', node_type='executor', status='published', params=params)])
    return Plan.objects.create(
        name=f'sync-plan-{code}', root_suite=suite, status='published',
        run_status=run_status, trigger_type='time', cron_expr='30 10 * * *')


def _make_run(plan, status):
    """造一条属于该 Plan 的运行记录（``SuiteRun.plan`` 是稳定的归属键）。"""
    case = plan.root_suite.cases.first()
    return SuiteRun.objects.create(
        plan=plan, suite=plan.root_suite,
        symbol=case.params['symbol_scope']['symbol_codes'][0],
        status=status, event_queue=[])


class PlanRunStatusFromRunsTest(TestCase):
    """按执行事实推导状态。"""

    def test_no_runs_returns_none(self):
        plan = _make_plan()
        self.assertIsNone(plan_run_status_from_runs(plan))

    def test_active_run_yields_running(self):
        plan = _make_plan()
        _make_run(plan, 'pending')
        _make_run(plan, 'running')
        self.assertEqual(plan_run_status_from_runs(plan), 'running')

    def test_all_completed_yields_done(self):
        plan = _make_plan()
        _make_run(plan, 'completed')
        self.assertEqual(plan_run_status_from_runs(plan), 'done')

    def test_failed_run_yields_interrupt(self):
        plan = _make_plan()
        _make_run(plan, 'completed')
        _make_run(plan, 'failed')
        self.assertEqual(plan_run_status_from_runs(plan), 'interrupt')


class ReconcilePlanRunStatusTest(TestCase):
    """归一写入与幂等。"""

    def test_reconcile_sets_done_and_is_idempotent(self):
        plan = _make_plan(run_status='running')
        _make_run(plan, 'completed')

        first = reconcile_plan_run_status(plan)
        second = reconcile_plan_run_status(plan)

        plan.refresh_from_db()
        self.assertTrue(first['changed'])
        self.assertEqual((first['from'], first['to']), ('running', 'done'))
        self.assertFalse(second['changed'])
        self.assertEqual(plan.run_status, 'done')

    def test_reconcile_keeps_status_without_runs(self):
        plan = _make_plan(run_status='new')
        result = reconcile_plan_run_status(plan)

        plan.refresh_from_db()
        self.assertFalse(result['changed'])
        self.assertEqual(plan.run_status, 'new')

    def test_batch_reconcile_scans_only_active_plans(self):
        stuck = _make_plan(run_status='running', code='000002')
        _make_run(stuck, 'completed')
        untouched = _make_plan(run_status='new', code='000003')

        stats = reconcile_all_plan_run_statuses()

        stuck.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(stuck.run_status, 'done')
        self.assertEqual(untouched.run_status, 'new')
        self.assertEqual(stats['changed_count'], 1)
        self.assertEqual(stats['changed'][0]['plan_id'], stuck.pk)

    def test_batch_reconcile_accepts_explicit_plan_ids(self):
        plan = _make_plan(run_status='running', code='000004')
        _make_run(plan, 'failed')

        stats = reconcile_all_plan_run_statuses(plan_ids=[plan.pk])

        plan.refresh_from_db()
        self.assertEqual(plan.run_status, 'interrupt')
        self.assertEqual(stats['checked'], 1)


class ExecutionReconcileWiringTest(TestCase):
    """执行端接线：每次执行结束（成功/失败）都归一。"""

    @staticmethod
    def _service():
        from runner.service import PlanExecutionService

        return PlanExecutionService(risk_controller=None, data_context_builder=None)

    def test_plan_marked_done_after_successful_execution(self):
        plan = _make_plan(run_status='running')

        self._service().run(plan, '000001')

        plan.refresh_from_db()
        self.assertEqual(plan.run_status, 'done')
        self.assertEqual(SuiteRun.objects.filter(plan=plan).count(), 1)

    def test_plan_stays_running_while_another_run_active(self):
        plan = _make_plan(run_status='running')
        _make_run(plan, 'pending')

        self._service().run(plan, '000001')

        plan.refresh_from_db()
        self.assertEqual(plan.run_status, 'running')   # 仍有活跃运行 → 不误判完成

    def test_claimed_path_also_reconciles(self):
        from apps.execution.services import create_suite_run

        plan = _make_plan(run_status='running')
        run = create_suite_run(plan, '000001')

        self._service().run(plan, '000001', {'suite_run_id': run.pk})

        run.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(plan.run_status, 'done')

    def test_reconcile_failure_does_not_break_execution(self):
        plan = _make_plan(run_status='running')

        with mock.patch('apps.execution.state_sync.reconcile_plan_run_status',
                        side_effect=RuntimeError('db down')):
            log = self._service().run(plan, '000001')

        plan.refresh_from_db()
        self.assertIsNotNone(log)                        # 执行不受影响
        self.assertEqual(plan.run_status, 'running')     # 归一失败，保持原状


class PlanRestartTest(TestCase):
    """``start_plan`` 放开重启（此前跑完即不可再启动）。"""

    def test_can_restart_after_done(self):
        plan = _make_plan(run_status='done')
        start_plan(plan)
        self.assertEqual(plan.run_status, 'running')

    def test_can_restart_after_interrupt(self):
        plan = _make_plan(run_status='interrupt')
        start_plan(plan)
        self.assertEqual(plan.run_status, 'running')

    def test_still_rejects_when_already_running(self):
        plan = _make_plan(run_status='running')
        with self.assertRaises(StateMachineError):
            start_plan(plan)


class SchedulerStateSyncTest(TestCase):
    """调度器启动期归一接线。"""

    @staticmethod
    def _command(stdout, stderr):
        from apps.plans.management.commands.run_scheduler import Command

        return Command(stdout=stdout, stderr=stderr)

    def test_startup_reconciles_and_reports(self):
        plan = _make_plan(run_status='running', code='000005')
        _make_run(plan, 'completed')
        stdout = StringIO()

        stats = self._command(stdout, StringIO())._reconcile_plan_states()

        plan.refresh_from_db()
        self.assertEqual(plan.run_status, 'done')
        self.assertEqual(stats['changed_count'], 1)
        self.assertIn('[state-sync] 已归一 1 个 Plan', stdout.getvalue())

    def test_startup_failure_does_not_block_scheduler(self):
        stderr = StringIO()
        with mock.patch('apps.execution.state_sync.reconcile_all_plan_run_statuses',
                        side_effect=RuntimeError('db down')):
            stats = self._command(StringIO(), stderr)._reconcile_plan_states()

        self.assertIsNone(stats)
        self.assertIn('不阻断调度', stderr.getvalue())
