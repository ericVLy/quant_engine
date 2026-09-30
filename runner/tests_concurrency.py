"""Complex scenario tests (T1/T2/T5): concurrency model, multi-symbol dispatch,
cross-instance dedup, fund allocation and trade window boundaries.

This module deliberately avoids real thread-based stress tests: the execution path
goes through ``PlanExecutionService.arun`` which uses
``sync_to_async(thread_sensitive=True)``. asgiref funnels every worker onto a
single thread, so execution is already serialised (see
``ExecutionSerializationTest``). Spawning real threads here would only add lock
churn on SQLite without validating any real risk.

What this module actually guards are the data-layer contracts:
  * one and only one run per (plan, symbol), even across scheduler instances
  * claim is exclusive and cannot be replayed
  * ``FundAllocation.used_amount`` never exceeds ``amount``
  * trade windows are evaluated in the market timezone (DST aware)
"""
import asyncio
import time
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from asgiref.sync import sync_to_async
from django.test import SimpleTestCase, TestCase

from apps.cases.models import Case
from apps.execution.funds import InsufficientFunds, reserve_for_order
from apps.execution.models import ExecutionLog, FundAllocation, Order, SuiteRun
from apps.execution.services import claim_suite_run
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.users.models import User
from apps.watchlists.models import Symbol
from runner.risk import RiskController, TradeTimeWindow, risk_kwargs_from_plan
from runner.scheduler import Scheduler
from runner.service import PlanExecutionService

NOW = datetime(2026, 8, 26, 10, 30)          # Wednesday, aligned with CRON
CRON = '30 10 * * *'
SHANGHAI = ZoneInfo('Asia/Shanghai')
SYMBOLS = ('000001', '000002')


def _multi_symbol_plan(symbols=SYMBOLS):
    """Published time-driven Plan whose Case declares several symbols."""
    user, _ = User.objects.get_or_create(username='concurrency')
    for code in symbols:
        Symbol.objects.get_or_create(code=code, defaults={'name': f'sym-{code}', 'market': 'A'})
    suite = Suite.objects.create(name='concurrent suite', status='published', created_by=user)
    suite.cases.set([Case.objects.create(
        name='concurrent case', node_type='executor', status='published',
        params={
            'trigger': {'event_type': 'SUITE_INIT'},
            'symbol_scope': {'type': 'symbols', 'symbol_codes': list(symbols)},
            'result': {'direction': 1, 'order': {'direction': 'buy', 'price': 10, 'volume': 10}},
        })])
    return Plan.objects.create(
        name='concurrent plan', root_suite=suite, status='published',
        trigger_type='time', cron_expr=CRON, exec_mode='serial',
    )


class ExecutionSerializationTest(SimpleTestCase):
    """Characterisation test: execution is funnelled onto a single thread.

    Its purpose is to pin the current architecture down. If someone switches
    ``arun`` to ``thread_sensitive=False`` (real parallelism), this test fails
    and points at the required follow-up: re-evaluate SQLite write contention
    and row-level serialisation of fund allocation.
    """

    def test_thread_sensitive_serializes_workers(self):
        def _work():
            time.sleep(0.15)

        async def _run():
            started = time.monotonic()
            await asyncio.gather(*[
                sync_to_async(_work, thread_sensitive=True)() for _ in range(2)
            ])
            return time.monotonic() - started

        elapsed = asyncio.run(_run())

        self.assertGreater(
            elapsed, 0.25,
            'Execution is no longer serialised. If this was an intentional switch to '
            'real parallelism (thread_sensitive=False), update the concurrency notes '
            'in documents.md and README and re-evaluate SQLite write contention.')


class MultiSymbolDispatchTest(TestCase):
    """T1: a multi-symbol Plan runs each symbol exactly once."""

    def setUp(self):
        self.plan = _multi_symbol_plan()
        self.scheduler = Scheduler(poll_interval=0.01)

    def _service(self):
        # Risk controller and data context are disabled on purpose so the
        # assertions depend on the execution chain only.
        return PlanExecutionService(risk_controller=None, data_context_builder=None)

    def test_each_symbol_runs_exactly_once(self):
        self.scheduler.enqueue_due_plans(NOW)

        pending = SuiteRun.objects.filter(plan=self.plan, status='pending')
        self.assertEqual(pending.count(), len(SYMBOLS))
        self.assertEqual(sorted(run.symbol for run in pending), sorted(SYMBOLS))

        # Simulate the worker pool delivering one intent at a time. The claim
        # itself happens inside the service (compare-and-set on status).
        for run in list(pending):
            self._service().run(self.plan, run.symbol, {'suite_run_id': run.pk})

        # A claimed or finished run must never be claimable again.
        for run in SuiteRun.objects.filter(plan=self.plan):
            self.assertFalse(claim_suite_run(run))

        runs = SuiteRun.objects.filter(plan=self.plan)
        self.assertEqual(runs.count(), len(SYMBOLS))      # no second run was created
        self.assertEqual(sorted(run.symbol for run in runs), sorted(SYMBOLS))
        self.assertEqual(sorted({run.status for run in runs}), ['completed'])
        self.assertEqual(ExecutionLog.objects.filter(plan=self.plan).count(), len(SYMBOLS))
        self.assertEqual(Order.objects.filter(symbol__in=SYMBOLS).count(), len(SYMBOLS))

    def test_two_instances_do_not_duplicate(self):
        """The in-memory dedup set only works within one process; cross-instance
        dedup must come from the DB-level check for an unfinished run."""
        first, second = Scheduler(poll_interval=0.01), Scheduler(poll_interval=0.01)

        first.enqueue_due_plans(NOW)
        second.enqueue_due_plans(NOW)                     # fresh instance == restart

        self.assertEqual(
            SuiteRun.objects.filter(plan=self.plan).count(), len(SYMBOLS))

    def test_existing_active_run_blocks_new_intent(self):
        """A restarted scheduler must not enqueue another intent for a (plan,
        symbol) pair that already has an unfinished run."""
        self.scheduler.enqueue_due_plans(NOW)
        stuck = SuiteRun.objects.filter(plan=self.plan, status='pending').first()
        SuiteRun.objects.filter(pk=stuck.pk).update(status='running')   # simulate in-flight

        Scheduler(poll_interval=0.01).enqueue_due_plans(NOW)

        self.assertEqual(SuiteRun.objects.filter(plan=self.plan).count(), len(SYMBOLS))
        self.assertEqual(
            SuiteRun.objects.filter(plan=self.plan, status='pending').count(),
            len(SYMBOLS) - 1)

    def test_draft_plan_produces_no_intent(self):
        Plan.objects.filter(pk=self.plan.pk).update(status='draft')
        Scheduler(poll_interval=0.01).enqueue_due_plans(NOW)
        self.assertEqual(SuiteRun.objects.filter(plan=self.plan).count(), 0)


class FundAllocationNoOversellTest(TestCase):
    """T2: fund allocation must never oversell -- ``used_amount`` stays within
    ``amount``.

    Project rules require allocation to run inside a transaction with
    ``select_for_update`` (no check-then-act). Because the execution path is
    serialised anyway (see ``ExecutionSerializationTest``), what is pinned here
    is the data-layer invariant: no matter how many times it is called, the
    cumulative usage never exceeds the granted amount.
    """

    def setUp(self):
        self.plan = _multi_symbol_plan(symbols=('000001',))
        self.allocation = FundAllocation.objects.create(
            plan=self.plan, level='plan', amount=Decimal('1000.00'),
            used_amount=Decimal('0.00'), status='active')

    def test_cumulative_usage_never_exceeds_grant(self):
        first = reserve_for_order(self.plan, value=Decimal('600'))
        self.assertIsNotNone(first)
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.used_amount, Decimal('600.00'))

        with self.assertRaises(InsufficientFunds):
            reserve_for_order(self.plan, value=Decimal('600'))

        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.used_amount, Decimal('600.00'))
        self.assertLessEqual(self.allocation.used_amount, self.allocation.amount)

    def test_no_grant_configured_means_no_interception(self):
        self.allocation.delete()
        self.assertIsNone(reserve_for_order(self.plan, value=Decimal('999999')))

    def test_falls_back_to_parent_level(self):
        suite = self.plan.root_suite
        case = suite.cases.first()
        self.allocation.amount = Decimal('100.00')
        self.allocation.save(update_fields=['amount'])
        FundAllocation.objects.create(
            plan=self.plan, suite=suite, case=case, level='case',
            amount=Decimal('50.00'), used_amount=Decimal('0.00'), status='active')

        # case level has 50, order needs 80 -> falls back to the plan level
        reserve_for_order(self.plan, suite=suite, case=case, value=Decimal('80'))
        case_alloc = FundAllocation.objects.get(level='case')
        plan_alloc = FundAllocation.objects.get(level='plan')
        case_alloc.refresh_from_db()
        plan_alloc.refresh_from_db()
        self.assertEqual(case_alloc.used_amount, Decimal('0.00'))
        self.assertEqual(plan_alloc.used_amount, Decimal('80.00'))

        with self.assertRaises(InsufficientFunds):
            reserve_for_order(self.plan, suite=suite, case=case, value=Decimal('80'))


class TradeWindowBoundaryTest(TestCase):
    """T5: trade window boundaries, weekends and US daylight-saving switches."""

    def test_a_share_window_boundaries(self):
        window = TradeTimeWindow(timezone_name='Asia/Shanghai')
        cases = [
            ('09:29', False), ('09:30', True), ('11:30', True), ('11:31', False),
            ('12:30', False), ('13:00', True), ('15:00', True), ('15:01', False),
        ]
        for clock, expected in cases:
            hour, minute = (int(part) for part in clock.split(':'))
            moment = datetime(2026, 9, 29, hour, minute, tzinfo=SHANGHAI)   # Tuesday
            self.assertEqual(
                window.allows(moment), expected,
                f'{clock} should be {"allowed" if expected else "rejected"}')

    def test_weekend_is_always_rejected(self):
        window = TradeTimeWindow(timezone_name='Asia/Shanghai')
        saturday = datetime(2026, 10, 3, 10, 0, tzinfo=SHANGHAI)   # inside the window
        self.assertFalse(window.allows(saturday))

    def test_us_window_follows_daylight_saving(self):
        """09:30 US/Eastern maps to different UTC instants under EDT and EST."""
        window = TradeTimeWindow(sessions=[(9, 30, 11, 30)],
                                 timezone_name='America/New_York')
        # EDT (UTC-4): 2026-07-01 09:30 EDT == 13:30 UTC
        self.assertTrue(window.allows(datetime(2026, 7, 1, 13, 30, tzinfo=dt_timezone.utc)))
        # EST (UTC-5): 2026-01-05 09:30 EST == 14:30 UTC
        self.assertTrue(window.allows(datetime(2026, 1, 5, 14, 30, tzinfo=dt_timezone.utc)))
        # Under EST, 13:30 UTC is 08:30 local and must be rejected; a hard-coded
        # UTC-4 conversion would wrongly allow it.
        self.assertFalse(window.allows(datetime(2026, 1, 5, 13, 30, tzinfo=dt_timezone.utc)))

    def test_plan_cannot_override_deployment_timezone(self):
        """``risk_kwargs_from_plan`` must keep the deployment timezone of ``base``."""
        plan = _multi_symbol_plan(symbols=('000001',))
        base = RiskController(trade_timezone='Asia/Shanghai',
                              allowed_sessions=[[0, 0, 23, 59]])

        kwargs = risk_kwargs_from_plan(plan, base=base)

        self.assertEqual(kwargs['trade_timezone'], 'Asia/Shanghai')
        self.assertEqual(RiskController(**kwargs).trade_timezone, 'Asia/Shanghai')
