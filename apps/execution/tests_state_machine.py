"""Case / Suite / Plan 运行状态机 + 资金校验测试。"""

from decimal import Decimal

from django.test import TestCase

from apps.cases.models import Case
from apps.suites.models import Suite
from apps.plans.models import Plan
from apps.execution.models import AccountFundConfig
from apps.execution.state_machine import (
    StateMachineError,
    start_case, complete_case, fail_case,
    start_suite, complete_suite, interrupt_suite,
    start_plan, stop_plan, complete_plan,
    validate_plan_capital, validate_suite_joining_plan,
)


def make_plan(name='P1', run_status='new', account_id='', allocated_capital=None, suite_start_mode='manual'):
    """Create a plan with a root suite (required by model)."""
    suite = Suite.objects.create(name=f'{name}_root', run_status='new')
    plan = Plan.objects.create(
        name=name, run_status=run_status, account_id=account_id,
        allocated_capital=allocated_capital, suite_start_mode=suite_start_mode,
        root_suite=suite,
    )
    return plan, suite


class CaseStateMachineTest(TestCase):
    def setUp(self):
        self.suite = Suite.objects.create(name='S1', run_status='running')
        self.case = Case.objects.create(name='C1', node_type='signal', run_status='new')
        self.suite.cases.add(self.case)

    def test_start_case(self):
        start_case(self.case)
        self.assertEqual(self.case.run_status, 'running')

    def test_complete_case(self):
        start_case(self.case)
        complete_case(self.case)
        self.assertEqual(self.case.run_status, 'done')

    def test_fail_case(self):
        start_case(self.case)
        fail_case(self.case)
        self.assertEqual(self.case.run_status, 'failed')

    def test_case_cannot_start_when_suite_not_running(self):
        self.suite.run_status = 'new'
        self.suite.save()
        with self.assertRaises(StateMachineError):
            start_case(self.case)

    def test_case_cannot_complete_when_not_running(self):
        with self.assertRaises(StateMachineError):
            complete_case(self.case)

    def test_case_cannot_fail_when_not_running(self):
        with self.assertRaises(StateMachineError):
            fail_case(self.case)

    def test_case_not_in_suite_cannot_run(self):
        orphan = Case.objects.create(name='Orphan', node_type='signal')
        with self.assertRaises(StateMachineError):
            start_case(orphan)


class SuiteStateMachineTest(TestCase):
    def setUp(self):
        self.plan, self.suite = make_plan(run_status='running')
        self.case1 = Case.objects.create(name='C1', node_type='signal', run_status='new')
        self.case2 = Case.objects.create(name='C2', node_type='signal', run_status='new')
        self.suite.cases.add(self.case1, self.case2)

    def test_start_suite(self):
        start_suite(self.suite)
        self.assertEqual(self.suite.run_status, 'running')

    def test_complete_suite(self):
        start_suite(self.suite)
        complete_suite(self.suite)
        self.assertEqual(self.suite.run_status, 'done')

    def test_interrupt_suite_stops_running_cases(self):
        start_suite(self.suite)
        start_case(self.case1)
        start_case(self.case2)
        interrupt_suite(self.suite)
        self.assertEqual(self.suite.run_status, 'interrupt')
        self.case1.refresh_from_db()
        self.case2.refresh_from_db()
        self.assertEqual(self.case1.run_status, 'failed')
        self.assertEqual(self.case2.run_status, 'failed')

    def test_suite_auto_completes_when_all_cases_done(self):
        start_suite(self.suite)
        start_case(self.case1)
        start_case(self.case2)
        complete_case(self.case1)
        complete_case(self.case2)
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.run_status, 'done')

    def test_suite_interrupts_when_case_fails(self):
        start_suite(self.suite)
        start_case(self.case1)
        fail_case(self.case1)
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.run_status, 'interrupt')

    def test_suite_cannot_start_when_plan_not_running(self):
        self.plan.run_status = 'new'
        self.plan.save()
        with self.assertRaises(StateMachineError):
            start_suite(self.suite)


class PlanStateMachineTest(TestCase):
    def setUp(self):
        self.plan, self.suite = make_plan(run_status='new')

    def test_start_plan(self):
        start_plan(self.plan)
        self.assertEqual(self.plan.run_status, 'running')

    def test_stop_plan(self):
        start_plan(self.plan)
        stop_plan(self.plan)
        self.assertEqual(self.plan.run_status, 'interrupt')

    def test_complete_plan(self):
        start_plan(self.plan)
        complete_plan(self.plan)
        self.assertEqual(self.plan.run_status, 'done')

    def test_plan_auto_completes_when_all_suites_done(self):
        start_plan(self.plan)
        start_suite(self.suite)
        complete_suite(self.suite)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.run_status, 'done')

    def test_plan_auto_starts_suite_in_auto_mode(self):
        self.plan.suite_start_mode = 'auto'
        self.plan.save()
        start_plan(self.plan)
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.run_status, 'running')

    def test_plan_does_not_auto_start_suite_in_manual_mode(self):
        self.plan.suite_start_mode = 'manual'
        self.plan.save()
        start_plan(self.plan)
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.run_status, 'new')

    def test_plan_cannot_start_when_not_new(self):
        self.plan.run_status = 'running'
        self.plan.save()
        with self.assertRaises(StateMachineError):
            start_plan(self.plan)


class CapitalValidationTest(TestCase):
    def setUp(self):
        self.cfg = AccountFundConfig.objects.create(
            account_id='ACC001',
            total_capital=Decimal('100000'),
        )

    def test_plan_capital_within_available(self):
        plan = Plan(name='P1', account_id='ACC001', allocated_capital=Decimal('50000'),
                    root_suite=Suite.objects.create(name='tmp'))
        validate_plan_capital(plan)

    def test_plan_capital_exceeds_available(self):
        s = Suite.objects.create(name='tmp')
        Plan.objects.create(name='P0', account_id='ACC001', allocated_capital=Decimal('80000'), root_suite=s)
        plan = Plan(name='P1', account_id='ACC001', allocated_capital=Decimal('30000'),
                    root_suite=Suite.objects.create(name='tmp2'))
        with self.assertRaises(StateMachineError):
            validate_plan_capital(plan)

    def test_plan_capital_no_account_config(self):
        plan = Plan(name='P1', account_id='NO_ACCOUNT', allocated_capital=Decimal('1000'),
                    root_suite=Suite.objects.create(name='tmp'))
        with self.assertRaises(StateMachineError):
            validate_plan_capital(plan)

    def test_suite_joining_plan_within_available(self):
        plan = Plan.objects.create(name='P1', account_id='ACC001', allocated_capital=Decimal('50000'),
                                    root_suite=Suite.objects.create(name='tmp'))
        from apps.execution.models import FundAllocation
        FundAllocation.objects.create(plan=plan, level='plan', amount=Decimal('50000'), status='active')
        suite = Suite(name='S1', allocated_capital=Decimal('20000'))
        validate_suite_joining_plan(suite, plan)

    def test_suite_joining_plan_exceeds_available(self):
        root = Suite.objects.create(name='tmp')
        plan = Plan.objects.create(name='P1', account_id='ACC001', allocated_capital=Decimal('50000'),
                                    root_suite=root)
        Suite.objects.create(name='S0', allocated_capital=Decimal('40000'), parent=root)
        suite = Suite(name='S1', allocated_capital=Decimal('20000'))
        with self.assertRaises(StateMachineError):
            validate_suite_joining_plan(suite, plan)
