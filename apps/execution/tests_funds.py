# -*- coding: utf-8 -*-
"""分级资金占用链路测试：Plan 占用 → Suite 申请 → Case 申请 → 下单扣减。"""
from decimal import Decimal

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.funds import (
    FundError, InsufficientFunds, allocate_funds, refund, reserve_for_order,
)
from apps.execution.models import ExecutionLog, FundAllocation, Order
from apps.plans.models import Plan
from apps.suites.models import Suite
from runner.engine import SuiteRunner


class FundAllocationServiceTest(TestCase):
    def setUp(self):
        self.suite = Suite.objects.create(name='S', status='published')
        self.plan = Plan.objects.create(
            name='P', root_suite=self.suite, status='published',
            symbol_scope={'type': 'symbols'},
            account_id='acct-001', allocated_capital=Decimal('10000.00'),
        )
        self.child_suite = Suite.objects.create(
            name='S2', status='published', parent=self.suite,
        )
        self.case = Case.objects.create(
            name='C', node_type='executor', status='published',
            params={'trigger': {'event_type': 'SUITE_INIT'}},
        )

    def test_plan_level_requires_allocated_capital(self):
        self.plan.allocated_capital = None
        self.plan.save()
        with self.assertRaisesRegex(FundError, 'allocated_capital'):
            allocate_funds(self.plan, amount=1000)

    def test_plan_level_cannot_exceed_allocated_capital(self):
        with self.assertRaisesRegex(FundError, '不得超过'):
            allocate_funds(self.plan, amount=Decimal('10000.01'))
        allocation = allocate_funds(self.plan, amount=Decimal('10000.00'))
        self.assertEqual(allocation.level, 'plan')
        self.assertEqual(allocation.amount, Decimal('10000.00'))

    def test_suite_level_requires_plan_allocation(self):
        with self.assertRaisesRegex(FundError, '尚未设置占用资金'):
            allocate_funds(self.plan, suite=self.suite, amount=1000)

    def test_suite_allocations_cannot_exceed_plan_quota(self):
        allocate_funds(self.plan, amount=Decimal('5000'))
        allocate_funds(self.plan, suite=self.suite, amount=Decimal('4000'))
        with self.assertRaisesRegex(FundError, '超过 Plan 剩余额度'):
            allocate_funds(self.plan, suite=self.child_suite, amount=Decimal('2000'))
        # 同一 suite 重复申请走 update_or_create，不重复累计
        allocate_funds(self.plan, suite=self.suite, amount=Decimal('4500'))
        self.assertEqual(FundAllocation.objects.filter(level='suite').count(), 1)

    def test_case_level_requires_suite_allocation(self):
        allocate_funds(self.plan, amount=Decimal('5000'))
        with self.assertRaisesRegex(FundError, '尚未向 Plan 申请资金'):
            allocate_funds(self.plan, suite=self.suite, case=self.case, amount=100)

    def test_case_allocations_cannot_exceed_suite_quota(self):
        allocate_funds(self.plan, amount=Decimal('5000'))
        suite_alloc = allocate_funds(self.plan, suite=self.suite, amount=Decimal('2000'))
        allocate_funds(self.plan, suite=self.suite, case=self.case, amount=Decimal('1500'))
        other_case = Case.objects.create(
            name='C2', node_type='executor', status='published',
            params={'trigger': {'event_type': 'SUITE_INIT'}},
        )
        with self.assertRaisesRegex(FundError, '超过 Suite 剩余额度'):
            allocate_funds(self.plan, suite=self.suite, case=other_case, amount=Decimal('1000'))
        self.assertEqual(suite_alloc.level, 'suite')


class FundReserveTest(TestCase):
    def setUp(self):
        self.suite = Suite.objects.create(name='S', status='published')
        self.plan = Plan.objects.create(
            name='P', root_suite=self.suite, status='published',
            symbol_scope={'type': 'symbols'}, allocated_capital=Decimal('1000'),
        )
        self.case = Case.objects.create(
            name='C', node_type='executor', status='published',
            params={'trigger': {'event_type': 'SUITE_INIT'}},
        )

    def test_no_config_returns_none(self):
        self.assertIsNone(
            reserve_for_order(self.plan, suite=self.suite.pk,
                              case=self.case.pk, value=100)
        )

    def test_reserve_prefers_case_then_suite_then_plan(self):
        allocate_funds(self.plan, amount=Decimal('1000'))
        suite_alloc = allocate_funds(self.plan, suite=self.suite, amount=Decimal('600'))
        case_alloc = allocate_funds(
            self.plan, suite=self.suite, case=self.case, amount=Decimal('300'),
        )
        used = reserve_for_order(
            self.plan, suite=self.suite.pk, case=self.case.pk, value=Decimal('120'),
        )
        self.assertEqual(used.pk, case_alloc.pk)
        self.assertEqual(used.used_amount, Decimal('120'))
        suite_alloc.refresh_from_db()
        self.assertEqual(suite_alloc.used_amount, Decimal('0'))
        # case 额度用尽后回退 suite 级
        reserve_for_order(
            self.plan, suite=self.suite.pk, case=self.case.pk, value=Decimal('180'),
        )
        case_alloc.refresh_from_db()
        self.assertEqual(case_alloc.used_amount, Decimal('300'))
        used3 = reserve_for_order(
            self.plan, suite=self.suite.pk, case=self.case.pk, value=Decimal('50'),
        )
        self.assertEqual(used3.pk, suite_alloc.pk)
        suite_alloc.refresh_from_db()
        self.assertEqual(suite_alloc.used_amount, Decimal('50'))

    def test_insufficient_funds_raises(self):
        allocate_funds(self.plan, amount=Decimal('100'))
        with self.assertRaises(InsufficientFunds):
            reserve_for_order(self.plan, value=Decimal('100.01'))

    def test_refund_releases_used_amount(self):
        allocation = allocate_funds(self.plan, amount=Decimal('100'))
        reserve_for_order(self.plan, value=Decimal('80'))
        refund(allocation, Decimal('80'))
        allocation.refresh_from_db()
        self.assertEqual(allocation.used_amount, Decimal('0'))


class EngineFundEnforcementTest(TestCase):
    """引擎下单时按 case → suite → plan 扣减额度。"""

    def _make_case(self, price='12.34', volume=100):
        return Case.objects.create(
            name='Buy', node_type='executor', status='published',
            params={
                'trigger': {'event_type': 'SUITE_INIT'},
                'result': {'direction': 1, 'order': {
                    'direction': 'buy', 'price': price, 'volume': volume,
                }},
            },
        )

    def _suite_plan(self):
        suite = Suite.objects.create(name='S', status='published')
        plan = Plan.objects.create(
            name='P', root_suite=suite, status='published',
            symbol_scope={'type': 'symbols'}, allocated_capital=Decimal('10000'),
        )
        return suite, plan

    def test_order_reserves_from_case_allocation(self):
        suite, plan = self._suite_plan()
        case = self._make_case()  # 12.34 * 100 = 1234
        suite.cases.add(case)
        allocate_funds(plan, amount=Decimal('5000'))
        suite_alloc = allocate_funds(plan, suite=suite, amount=Decimal('2000'))
        case_alloc = allocate_funds(plan, suite=suite, case=case, amount=Decimal('1300'))

        log = SuiteRunner().run(plan, '000001')

        self.assertEqual(log.status, 'success')
        order = Order.objects.get(log=log)
        self.assertEqual(order.fund_allocation.pk, case_alloc.pk)
        case_alloc.refresh_from_db()
        self.assertEqual(case_alloc.used_amount, Decimal('1234.00'))
        suite_alloc.refresh_from_db()
        self.assertEqual(suite_alloc.used_amount, Decimal('0'))

    def test_insufficient_all_levels_rejects_run(self):
        suite, plan = self._suite_plan()
        plan.allocated_capital = Decimal('1000')
        plan.save()
        case = self._make_case()  # 需 1234
        suite.cases.add(case)
        allocate_funds(plan, amount=Decimal('1000'))
        suite_alloc = allocate_funds(plan, suite=suite, amount=Decimal('800'))
        allocate_funds(plan, suite=suite, case=case, amount=Decimal('500'))

        with self.assertRaisesRegex(Exception, '资金占用失败'):
            SuiteRunner().run(plan, '000001')

        order = Order.objects.get(symbol='000001')
        self.assertEqual(order.status, 'rejected')
        self.assertIn('资金额度不足', order.last_error)
        suite_alloc.refresh_from_db()
        self.assertEqual(suite_alloc.used_amount, Decimal('0'))
        self.assertEqual(ExecutionLog.objects.get(symbol='000001').status, 'failed')

    def test_case_shortfall_falls_back_to_suite_funds(self):
        """case 额度不足时使用 suite 申请的资金（case 只能调用 suite 的资金）。"""
        suite, plan = self._suite_plan()
        case = self._make_case()  # 需 1234
        suite.cases.add(case)
        allocate_funds(plan, amount=Decimal('5000'))
        suite_alloc = allocate_funds(plan, suite=suite, amount=Decimal('2000'))
        case_alloc = allocate_funds(plan, suite=suite, case=case, amount=Decimal('1000'))

        log = SuiteRunner().run(plan, '000001')

        self.assertEqual(log.status, 'success')
        order = Order.objects.get(log=log)
        self.assertEqual(order.fund_allocation.pk, suite_alloc.pk)
        suite_alloc.refresh_from_db()
        self.assertEqual(suite_alloc.used_amount, Decimal('1234.00'))
        case_alloc.refresh_from_db()
        self.assertEqual(case_alloc.used_amount, Decimal('0'))

    def test_no_allocation_keeps_legacy_behavior(self):
        suite, plan = self._suite_plan()
        case = self._make_case()
        suite.cases.add(case)
        log = SuiteRunner().run(plan, '000001')
        self.assertEqual(log.status, 'success')
        order = Order.objects.get(log=log)
        self.assertIsNone(order.fund_allocation)
