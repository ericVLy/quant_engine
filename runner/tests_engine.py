"""风控（R-08）、热加载注册中心（R-09）与引擎数据集成测试。"""

from datetime import date, datetime, time as dtime

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.models import ExecutionLog
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.watchlists.models import Symbol

from .engine import SuiteRunner
from .fixture import DataContextBuilder
from .registry import PlanRegistry
from .risk import DailyLimitPolicy, PositionPolicy, RiskDecision, TradeTimeWindow


class PositionPolicyTest(TestCase):
    def test_long_only_rejects_sell(self):
        policy = PositionPolicy(mode='long_only')
        decision = policy.check({'direction': 'sell', 'price': 10, 'volume': 5})
        self.assertFalse(decision.allowed)
        self.assertIn('long_only', decision.reason)

    def test_long_only_allows_buy(self):
        policy = PositionPolicy(mode='long_only')
        self.assertTrue(policy.check({'direction': 'buy', 'price': 10, 'volume': 5}).allowed)

    def test_max_volume_limit(self):
        policy = PositionPolicy(mode='both', max_volume=10)
        self.assertFalse(policy.check({'direction': 'buy', 'price': 1, 'volume': 11}).allowed)
        self.assertTrue(policy.check({'direction': 'buy', 'price': 1, 'volume': 10}).allowed)


class TradeTimeWindowTest(TestCase):
    def test_allows_during_session(self):
        window = TradeTimeWindow(sessions=[(9, 30, 11, 30)])
        self.assertTrue(window.allows(datetime(2026, 9, 3, 10, 0)))  # 周四

    def test_rejects_outside_session(self):
        window = TradeTimeWindow(sessions=[(9, 30, 11, 30)])
        self.assertFalse(window.allows(datetime(2026, 9, 3, 15, 0)))

    def test_rejects_weekend(self):
        window = TradeTimeWindow()
        self.assertFalse(window.allows(datetime(2026, 9, 5, 10, 0)))  # 周六


class DailyLimitPolicyTest(TestCase):
    def test_no_limit_allows(self):
        policy = DailyLimitPolicy(max_daily_value=None)
        self.assertTrue(policy.check({'direction': 'buy', 'price': 100, 'volume': 1000}).allowed)

    def test_blocks_over_daily_limit(self):
        policy = DailyLimitPolicy(max_daily_value=1000)
        decision = policy.check({'direction': 'buy', 'price': 20, 'volume': 100})  # 2000 > 1000
        self.assertFalse(decision.allowed)
        self.assertIn('每日累计金额', decision.reason)

    def test_blocks_zero_volume_in_position_policy(self):
        policy = PositionPolicy(mode='both')
        self.assertFalse(policy.check({'direction': 'buy', 'price': 1, 'volume': 0}).allowed)

    def test_risk_decision_shape(self):
        decision = RiskDecision(True, 'ok')
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, 'ok')


class PlanRegistryTest(TestCase):
    def setUp(self):
        PlanRegistry._plans = {}

    def test_refresh_and_snapshot(self):
        suite = Suite.objects.create(name='热加载 Suite', status='published')
        plan = Plan.objects.create(
            name='热加载 Plan', root_suite=suite, status='published', version=2,
            exec_mode='parallel', retry_policy={'max_retries': 2},
        )
        PlanRegistry.refresh(plan)
        entry = PlanRegistry.get(plan.pk)
        self.assertEqual(entry['version'], 2)
        self.assertEqual(entry['snapshot']['exec_mode'], 'parallel')
        self.assertEqual(PlanRegistry.get_snapshot(plan.pk)['version'], 2)

    def test_published_plans_self_healing_from_db(self):
        suite = Suite.objects.create(name='DB Suite', status='published')
        Plan.objects.create(name='DB Plan', root_suite=suite, status='published', trigger_type='time')
        planning = list(PlanRegistry.published_plans())
        self.assertTrue(any(p.name == 'DB Plan' for p in planning))

    def test_refresh_invalidates_on_version_change(self):
        suite = Suite.objects.create(name='版本 Suite', status='published')
        plan = Plan.objects.create(name='版本 Plan', root_suite=suite, status='published', version=1)
        PlanRegistry.refresh(plan)
        plan.version = 2
        PlanRegistry.refresh(plan)
        self.assertEqual(PlanRegistry.get(plan.pk)['version'], 2)


class EngineDataRunTest(TestCase):
    def test_runner_builds_data_context_and_executes_factor_case(self):
        symbol = Symbol.objects.create(code='000001', name='测试', market='A')
        suite = Suite.objects.create(name='因子 Suite', status='published')
        case = Case.objects.create(
            name='均线信号', node_type='signal', status='published',
            params={
                'trigger': {'event_type': 'SUITE_INIT'},
                'node_type': 'signal',
                'indicator': 'ma',
                'period': 5,
                # 声明式 result 作为数据为空时的兜底（本用例走统一入口 normal 路径）
            },
        )
        suite.cases.add(case)
        plan = Plan.objects.create(
            name='因子 Plan', root_suite=suite, status='published',
            symbol_scope={'type': 'symbols', 'symbol_codes': ['000001']},
        )

        class StubBuilder(DataContextBuilder):
            def build(self, symbol, context=None, **kwargs):
                ctx = super().build(symbol, context=context, **kwargs)
                # 提供确定性的行情数据以驱动 MA 方向判定
                ctx['market_data'] = [{'close': float(10 + i)} for i in range(30)]
                return ctx

        runner = SuiteRunner(data_context_builder=StubBuilder())
        log = runner.run(plan, symbol.code)

        self.assertEqual(log.status, 'success')
        self.assertEqual(log.final_direction, 1)  # 价格高于 5 日均线 -> 做多
        snap = log.node_snapshots[str(case.pk)]
        self.assertIn('ma', snap)