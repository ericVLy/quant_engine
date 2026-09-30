"""Plan 级风控限额（F1）测试：映射、序列化校验、执行期生效、版本快照。

背景：``RiskController`` 早已实现这些策略，但生产装配
（``build_execution_service`` / ``run_scheduler``）没有任何入口能配置它们，
实际只有「交易时段」和「volume > 0」在生效——单笔/每日/总仓位限额形同虚设。
本文件守住「Plan 声明的限额必须真的拦截订单」这一契约。
"""
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cases.models import Case
from apps.execution.models import Order
from apps.plans.models import Plan, PlanVersion
from apps.plans.services import publish_plan, rollback_plan
from apps.suites.models import Suite
from apps.users.models import User
from apps.watchlists.models import Symbol
from runner.registry import PlanRegistry
from runner.risk import (
    RiskController, has_plan_risk_limits, plan_risk_limits, risk_kwargs_from_plan,
)
from runner.service import PlanExecutionService

#: 周二 10:00（落在默认 A 股交易时段内）。所有执行期用例固定"当前时间"，
#: 否则用例会随运行时刻（含周末）随机失败。
WEDDAY_10AM = datetime(2026, 9, 29, 10, 0)


def _executable_plan(name='risk-plan', order=None, **risk_kwargs):
    """建一个可直接执行的已发布 Plan（Case 订阅 SUITE_INIT，可带一笔订单）。"""
    user, _ = User.objects.get_or_create(username='risk-user')
    Symbol.objects.get_or_create(code='000001',
                                defaults={'name': '平安银行', 'market': 'A'})
    suite = Suite.objects.create(name=f'{name}-suite', status='published',
                                 created_by=user)
    params = {
        'trigger': {'event_type': 'SUITE_INIT'},
        'symbol_scope': {'type': 'symbols', 'symbol_codes': ['000001']},
    }
    if order:
        params['result'] = {
            'direction': 1 if order['direction'] == 'buy' else -1,
            'order': order,
        }
    suite.cases.set([Case.objects.create(
        name=f'{name}-case', node_type='executor', status='published', params=params)])
    return Plan.objects.create(
        name=name, root_suite=suite, status='published',
        trigger_type='manual', **risk_kwargs)


def _service():
    """带默认风控装配的执行服务（与 ``enable_risk=True`` 的生产形态一致）。"""
    return PlanExecutionService(risk_controller=RiskController())


class PlanRiskLimitMappingTest(TestCase):
    """Plan 字段 → ``RiskController`` 参数的映射。"""

    def test_plan_without_limits_reports_none(self):
        plan = _executable_plan()
        self.assertEqual(plan_risk_limits(plan), {})
        self.assertFalse(has_plan_risk_limits(plan))

    def test_default_position_mode_is_not_treated_as_declared(self):
        """默认 ``both`` 等同控制器默认值，不应被当成"用户配置过"。"""
        plan = _executable_plan()
        self.assertEqual(plan.risk_position_mode, 'both')
        self.assertFalse(has_plan_risk_limits(plan))

    def test_all_fields_map_to_constructor_kwargs(self):
        plan = _executable_plan(
            risk_position_mode='long_only', risk_max_order_volume=500,
            risk_max_order_value=Decimal('1234.50'), risk_max_daily_value=Decimal('99999'),
            risk_max_account_value=Decimal('500000'), risk_max_position_value=Decimal('88888'),
            risk_max_position_volume=777, risk_allowed_sessions=[[9, 30, 11, 30]],
        )

        limits = plan_risk_limits(plan)
        kwargs = risk_kwargs_from_plan(plan)

        self.assertTrue(has_plan_risk_limits(plan))
        self.assertEqual(len(limits), 8)
        self.assertEqual(kwargs['position_mode'], 'long_only')
        self.assertEqual(kwargs['max_volume'], 500)
        self.assertEqual(kwargs['max_value'], 1234.5)
        self.assertEqual(kwargs['max_daily_value'], 99999.0)
        self.assertEqual(kwargs['max_account_value'], 500000.0)
        self.assertEqual(kwargs['max_position_value'], 88888.0)
        self.assertEqual(kwargs['max_position_volume'], 777)
        self.assertEqual(kwargs['allowed_sessions'], [[9, 30, 11, 30]])

    def test_base_controller_settings_are_preserved_and_not_overridable(self):
        """时区与账户快照属于环境配置，Plan 不得覆盖。"""
        class Account:
            def get_account(self):
                return {'available': 1}

            def get_positions(self):
                return []

        base = RiskController(trade_timezone='Asia/Shanghai',
                              account_provider=Account())
        plan = _executable_plan(risk_max_order_value=Decimal('50'))

        kwargs = risk_kwargs_from_plan(plan, base=base)

        self.assertEqual(kwargs['trade_timezone'], 'Asia/Shanghai')
        self.assertIs(kwargs['account_provider'], base.account_provider)
        self.assertEqual(kwargs['allowed_sessions'], list(base.trade_window.sessions))

    def test_plan_sessions_override_base_window(self):
        base = RiskController()
        plan = _executable_plan(risk_allowed_sessions=[[0, 0, 23, 59]])

        self.assertEqual(risk_kwargs_from_plan(plan, base=base)['allowed_sessions'],
                         [[0, 0, 23, 59]])


class PlanRiskLimitSerializerTest(APITestCase):
    """API 层字段校验：限额是安全相关数值，非法值必须在入口就被拒。"""

    def setUp(self):
        self.user = User.objects.create_user('riskapi', password='x', is_staff=True)
        self.suite = Suite.objects.create(name='risk-api-suite', status='published')
        self.client.force_authenticate(self.user)

    def _payload(self, **overrides):
        data = {'name': '限额计划', 'root_suite': self.suite.id, 'trigger_type': 'manual'}
        data.update(overrides)
        return data

    def _create(self, **overrides):
        return self.client.post('/api/plans/', self._payload(**overrides), format='json')

    def test_accepts_valid_limits(self):
        response = self._create(
            risk_max_order_value='500.00', risk_max_order_volume=100,
            risk_position_mode='long_only', risk_allowed_sessions=[[9, 30, 11, 30]],
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['risk_max_order_value'], '500.00')
        self.assertEqual(response.data['risk_position_mode'], 'long_only')
        self.assertEqual(response.data['risk_allowed_sessions'], [[9, 30, 11, 30]])

    def test_rejects_non_positive_amounts(self):
        for field in ('risk_max_order_value', 'risk_max_daily_value',
                      'risk_max_account_value', 'risk_max_position_value'):
            for bad in ('0', '-1'):
                response = self._create(**{field: bad})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST,
                                 f'{field}={bad} 应被拒绝')
                self.assertIn(field, response.data)

    def test_rejects_non_positive_volume(self):
        for bad in (0, -5):
            response = self._create(risk_max_order_volume=bad)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_invalid_position_mode(self):
        response = self._create(risk_position_mode='both_ways')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('risk_position_mode', response.data)

    def test_sessions_nested_form_is_normalised(self):
        response = self._create(risk_allowed_sessions=[[[9, 30], [11, 30]]])

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['risk_allowed_sessions'], [[9, 30, 11, 30]])

    def test_sessions_reject_malformed_values(self):
        cases = [
            'not-a-list',
            [[9, 30]],
            [[9, 30, 11, 30, 12]],
            [[9, 30, 11, 'x']],
            [[9, 30, 11, 60]],
            [[25, 0, 26, 0]],
            [[11, 30, 9, 30]],
        ]
        for value in cases:
            response = self._create(risk_allowed_sessions=value)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST,
                             f'{value} 应被拒绝')
            self.assertIn('risk_allowed_sessions', response.data)

    def test_null_limits_mean_unlimited(self):
        response = self._create(risk_max_order_value=None, risk_allowed_sessions=None)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data['risk_max_order_value'])
        self.assertIsNone(response.data['risk_allowed_sessions'])

    def test_serializer_rejects_bool_masquerading_as_volume(self):
        from apps.plans.serializers import PlanSerializer

        serializer = PlanSerializer(data=self._payload(risk_max_order_volume=True))

        self.assertFalse(serializer.is_valid())
        self.assertIn('risk_max_order_volume', serializer.errors)


class PlanRiskLimitEnforcementTest(TestCase):
    """执行期：Plan 声明的限额必须**真的**拦住订单，且拦截后不下单。"""

    def _service(self, broker=None):
        return PlanExecutionService(broker=broker, risk_controller=RiskController())

    def _run(self, plan, service=None):
        with patch('runner.risk.timezone.localtime', return_value=WEDDAY_10AM):
            return (service or self._service()).run(plan, '000001')

    def test_order_value_limit_blocks_order(self):
        plan = _executable_plan(
            name='risk-value', order={'direction': 'buy', 'price': 10, 'volume': 10},
            risk_max_order_value=Decimal('50'))          # 本笔 100 > 50

        log = self._run(plan)

        self.assertEqual(log.status, 'blocked')
        self.assertEqual(log.error_code, 'RISK_BLOCKED')
        self.assertIn('金额', log.error_msg)

    def test_order_volume_limit_blocks_order(self):
        plan = _executable_plan(
            name='risk-volume', order={'direction': 'buy', 'price': 10, 'volume': 10},
            risk_max_order_volume=5)

        log = self._run(plan)

        self.assertEqual(log.status, 'blocked')
        self.assertIn('数量', log.error_msg)

    def test_long_only_blocks_sell(self):
        plan = _executable_plan(
            name='risk-long-only', order={'direction': 'sell', 'price': 10, 'volume': 10},
            risk_position_mode='long_only')

        log = self._run(plan)

        self.assertEqual(log.status, 'blocked')
        self.assertIn('long_only', log.error_msg)

    def test_plan_sessions_window_is_enforced(self):
        plan = _executable_plan(
            name='risk-sessions', order={'direction': 'buy', 'price': 10, 'volume': 10},
            risk_allowed_sessions=[[3, 0, 3, 1]])       # 固定"当前 10:00"不在窗口内

        log = self._run(plan)

        self.assertEqual(log.status, 'blocked')
        self.assertIn('交易时段', log.error_msg)

    def test_daily_limit_accumulates_amount_not_unit_price(self):
        from apps.execution.models import ExecutionLog, Order

        plan = _executable_plan(
            name='risk-daily', order={'direction': 'buy', 'price': 10, 'volume': 10},
            risk_max_daily_value=Decimal('500'))
        log_row = ExecutionLog.objects.create(
            plan=plan, symbol='000001', final_direction=1)
        pre = Order.objects.create(log=log_row, symbol='000001', direction='buy',
                                   price=Decimal('10'), volume=100, status='sent')
        # 固定"已用 1000"的当日存量：日期须与被 patch 的 localdate() 一致
        Order.objects.filter(pk=pre.pk).update(
            created_at=datetime(2026, 9, 29, 1, 0, tzinfo=dt_timezone.utc))

        log = self._run(plan)

        self.assertEqual(log.status, 'blocked')
        self.assertIn('每日累计金额', log.error_msg)

    def test_blocked_order_is_never_submitted_to_broker(self):
        class Broker:
            def __init__(self):
                self.submitted = []

            def submit_order(self, symbol, order_data):
                self.submitted.append(dict(order_data))
                return {'order_id': 'EXT-1'}

        broker = Broker()
        plan = _executable_plan(
            name='risk-nosubmit', order={'direction': 'buy', 'price': 10, 'volume': 10},
            risk_max_order_value=Decimal('50'))

        log = self._run(plan, service=self._service(broker=broker))

        self.assertEqual(log.status, 'blocked')
        self.assertEqual(broker.submitted, [])

    def test_plan_without_limits_is_unaffected(self):
        """未声明限额的 Plan 行为与改造前一致（不因新增字段被误拦）。"""
        plan = _executable_plan(
            name='risk-none', order={'direction': 'buy', 'price': 10, 'volume': 10})

        log = self._run(plan)

        self.assertEqual(log.status, 'success')
        self.assertEqual(Order.objects.filter(symbol='000001').count(), 1)

    def test_limits_are_isolated_per_plan(self):
        limited = _executable_plan(
            name='risk-iso-limited', order={'direction': 'buy', 'price': 10, 'volume': 10},
            risk_max_order_value=Decimal('50'))
        unlimited = _executable_plan(
            name='risk-iso-free', order={'direction': 'buy', 'price': 10, 'volume': 10})

        limited_log = self._run(limited)
        unlimited_log = self._run(unlimited)

        self.assertEqual(limited_log.status, 'blocked')
        self.assertEqual(unlimited_log.status, 'success')

    def test_claimed_path_also_uses_plan_limits(self):
        """持久化意向/认领路径同样应用 Plan 限额（不能只覆盖新建运行路径）。"""
        from apps.execution.services import create_suite_run

        plan = _executable_plan(
            name='risk-claimed', order={'direction': 'buy', 'price': 10, 'volume': 10},
            risk_max_order_value=Decimal('50'))
        run = create_suite_run(plan, '000001')

        with patch('runner.risk.timezone.localtime', return_value=WEDDAY_10AM):
            log = self._service().run(plan, '000001', {'suite_run_id': run.pk})

        run.refresh_from_db()
        self.assertEqual(log.status, 'blocked')
        self.assertEqual(log.error_code, 'RISK_BLOCKED')
        self.assertEqual(run.status, 'failed')


class PlanRiskSnapshotTest(TestCase):
    """发布快照与回滚：策略与风控限额必须来自**同一个版本**。"""

    def setUp(self):
        PlanRegistry._plans = {}

    def test_publish_snapshot_contains_risk_limits(self):
        plan = _executable_plan(name='snap', risk_max_order_value=Decimal('777'))

        publish_plan(plan)

        snapshot = PlanVersion.objects.get(
            plan=plan, version=plan.version).snapshot
        self.assertEqual(Decimal(str(snapshot['risk_max_order_value'])), Decimal('777'))
        for field in ('risk_position_mode', 'risk_max_order_volume',
                      'risk_max_daily_value', 'risk_allowed_sessions'):
            self.assertIn(field, snapshot)

    def test_rollback_restores_risk_limits(self):
        plan = _executable_plan(name='rb', risk_max_order_value=Decimal('777'))
        publish_plan(plan)                              # v2 快照含 777
        plan.risk_max_order_value = Decimal('1')
        plan.save(update_fields=['risk_max_order_value'])

        rollback_plan(plan, plan.version)                # 回滚到 v2 快照

        plan.refresh_from_db()
        self.assertEqual(plan.version, 3)                # 回滚产生新版本，不覆盖历史
        self.assertEqual(Decimal(str(plan.risk_max_order_value)), Decimal('777'))


class PlanCapitalValidationIntegrationTest(APITestCase):
    """Plan 占用资金校验的**集成**路径。

    背景：``PlanSerializer`` 曾定义两个同名 ``validate``，后者静默覆盖前者，
    导致 ``validate_plan_capital`` 成为死代码——"Plan 占用资金不得超过账户
    空闲资金"这条规则在 API 上从未真正生效。此类用例防止它再次退化。
    """

    def setUp(self):
        from apps.execution.models import AccountFundConfig

        self.user = User.objects.create_user('riskcap', password='x', is_staff=True)
        self.suite = Suite.objects.create(name='cap-suite', status='published')
        AccountFundConfig.objects.create(
            account_id='ACC-RISK', total_capital=Decimal('1000'))
        self.client.force_authenticate(self.user)

    def _payload(self, allocated):
        return {
            'name': '占用资金计划', 'root_suite': self.suite.id,
            'trigger_type': 'manual', 'account_id': 'ACC-RISK',
            'allocated_capital': allocated,
        }

    def test_api_rejects_allocation_exceeding_free_capital(self):
        response = self.client.post('/api/plans/', self._payload('5000'), format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('allocated_capital', response.data)

    def test_api_accepts_allocation_within_free_capital(self):
        response = self.client.post('/api/plans/', self._payload('500'), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class PlanRegistryRiskHotReloadTest(TestCase):
    """限额改动必须被注册中心识别（否则改了限额仍按旧值执行）。"""

    def setUp(self):
        PlanRegistry._plans = {}

    def test_snapshot_refreshes_when_risk_limit_changes(self):
        plan = _executable_plan(name='hot', risk_max_order_value=Decimal('100'))
        PlanRegistry.sync_from_database()
        first = PlanRegistry.get_snapshot(plan.pk)
        self.assertEqual(Decimal(str(first['risk_max_order_value'])), Decimal('100'))

        plan.risk_max_order_value = Decimal('200')
        plan.save(update_fields=['risk_max_order_value'])
        PlanRegistry.sync_from_database()

        second = PlanRegistry.get_snapshot(plan.pk)
        self.assertEqual(Decimal(str(second['risk_max_order_value'])), Decimal('200'))
