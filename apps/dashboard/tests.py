"""N-06 运行总览（Dashboard）测试：聚合口径、全表 vs 窗口、量纲与 N-05 卫生。

核心回归点：**统计在 DB 侧聚合**，不受前端 ``page_size`` 上限影响——旧实现是
前端拉最多 500 条执行记录自己算成功率，数据一多口径就会漂移。
"""
from datetime import datetime, time as dtime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.cases.models import Case
from apps.dashboard import services
from apps.dashboard.serializers import normalize
from apps.execution.models import (
    AccountFundConfig, Alert, ExecutionLog, Order, SuiteRun,
)
from apps.execution.recovery import recover_orphaned_runs
from apps.monitoring.models import IntradayPoint
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.users.models import User
from apps.watchlists.models import Symbol

SHANGHAI = ZoneInfo('Asia/Shanghai')


def _base_fixture(code='000001', run_status='running'):
    user = User.objects.create_user(username='dash', password='x', is_staff=True)
    symbol = Symbol.objects.create(code=code, name='测试标的', market='A')
    suite = Suite.objects.create(name='dash-suite', status='published', created_by=user)
    suite.cases.set([Case.objects.create(
        name='dash-case', node_type='executor', status='published',
        params={'trigger': {'event_type': 'SUITE_INIT'},
                'symbol_scope': {'type': 'symbols', 'symbol_codes': [code]}})])
    plan = Plan.objects.create(
        name='dash-plan', root_suite=suite, status='published', run_status=run_status,
        trigger_type='manual')
    return plan, symbol


def _make_run(plan, status, symbol='000001', created_at=None):
    run = SuiteRun.objects.create(
        plan=plan, suite=plan.root_suite, symbol=symbol, status=status, event_queue=[])
    if created_at is not None:
        SuiteRun.objects.filter(pk=run.pk).update(created_at=created_at)
    return run


class OverviewEmptyTest(TestCase):
    """空库：全 0，且比率返回 ``None`` 而不是 0（避免误读为「成功率 0%」）。"""

    def test_empty_database_returns_zeroed_snapshot(self):
        data = services.overview()

        self.assertEqual(data['execution']['window_total'], 0)
        self.assertIsNone(data['execution']['success_rate'])
        self.assertIsNone(data['execution']['avg_duration_ms'])
        self.assertEqual(data['execution']['active_runs'], 0)
        self.assertEqual(data['orders']['window_total'], 0)
        self.assertIsNone(data['orders']['notional_total'])
        self.assertEqual(data['funds'], {'configured': False})
        self.assertEqual(data['alerts']['open'], 0)
        self.assertEqual(data['config']['plans_published'], 0)

    def test_trend_on_empty_database_is_all_zero(self):
        series = services.execution_trend(days=5)

        self.assertEqual(len(series), 5)
        self.assertTrue(all(item['total'] == 0 for item in series))
        self.assertTrue(all(item['success_rate'] is None for item in series))


class OverviewMetricsTest(TestCase):
    """有数据时的口径。"""

    def setUp(self):
        self.plan, self.symbol = _base_fixture()
        self.now = timezone.now()
        self.recent = self.now - timedelta(hours=2)

    def test_window_metrics_and_success_rate_use_settled_runs_only(self):
        _make_run(self.plan, 'completed', created_at=self.recent)
        _make_run(self.plan, 'completed', created_at=self.recent)
        _make_run(self.plan, 'failed', created_at=self.recent)
        _make_run(self.plan, 'pending', created_at=self.recent)      # 未终结，不进分母

        data = services.overview()['execution']

        self.assertEqual(data['window_total'], 4)
        self.assertEqual(data['settled_total'], 3)                  # pending 不进分母
        self.assertEqual(data['success_rate'], 66.7)                # 2/3
        self.assertEqual(data['failure_rate'], 33.3)                # 1/3
        self.assertEqual(data['by_status']['pending'], 1)

    def test_counts_are_database_side_not_bounded_by_page_size(self):
        """核心回归：600 条运行必须被完整统计（旧前端实现只算最近 500 条）。"""
        SuiteRun.objects.bulk_create([
            SuiteRun(plan=self.plan, suite=self.plan.root_suite, symbol='000001',
                     status='completed', event_queue=[])
            for _ in range(600)
        ])

        data = services.overview()['execution']

        self.assertEqual(data['window_total'], 600)
        self.assertEqual(data['by_status']['completed'], 600)
        self.assertEqual(data['success_rate'], 100.0)

    def test_active_runs_is_full_table_not_window_scoped(self):
        """活跃运行回答「现在健康吗」，因此**不受窗口限制**。"""
        stale = self.now - timedelta(days=5)
        _make_run(self.plan, 'completed', created_at=self.recent)
        _make_run(self.plan, 'running', created_at=stale)             # 窗口外
        _make_run(self.plan, 'pending', created_at=stale)

        data = services.overview(window_days=1)['execution']

        self.assertEqual(data['window_total'], 1)                   # 窗口只含 1 条
        self.assertEqual(data['active_runs'], 2)                   # 活跃是全表事实
        self.assertEqual(data['running_runs'], 1)

    def test_avg_duration_uses_execution_log(self):
        log = ExecutionLog.objects.create(
            plan=self.plan, symbol='000001', final_direction=1, duration_ms=120)
        ExecutionLog.objects.filter(pk=log.pk).update(trigger_time=self.recent)

        self.assertEqual(services.overview()['execution']['avg_duration_ms'], 120)

    def test_order_notional_uses_amount_not_unit_price(self):
        """金额口径：``price × volume``，而不是 ``Sum('price')`` 单价之和。"""
        log = ExecutionLog.objects.create(
            plan=self.plan, symbol='000001', final_direction=1)
        for _ in range(2):
            order = Order.objects.create(
                log=log, symbol='000001', direction='buy',
                price=Decimal('10'), volume=100, status='sent')
            Order.objects.filter(pk=order.pk).update(created_at=self.recent)

        orders = services.overview()['orders']

        self.assertEqual(orders['window_total'], 2)
        self.assertEqual(orders['notional_total'], Decimal('2000'))   # 10×100×2
        self.assertEqual(orders['notional_direction']['buy'], Decimal('2000'))

    def test_unconfirmed_orders_use_same_criterion_as_recovery(self):
        log = ExecutionLog.objects.create(
            plan=self.plan, symbol='000001', final_direction=1)
        stale = Order.objects.create(
            log=log, symbol='000001', direction='buy', price=Decimal('10'),
            volume=10, status='pending')
        Order.objects.filter(pk=stale.pk).update(
            created_at=self.now - timedelta(seconds=7200))           # 2 小时前 > 1800s
        Order.objects.create(log=log, symbol='000001', direction='buy',
                             price=Decimal('10'), volume=10, status='filled')

        self.assertEqual(services.overview()['orders']['unconfirmed'], 1)

    def test_config_health_exposes_plan_run_status_distribution(self):
        data = services.overview()['config']

        self.assertEqual(data['plans_published'], 1)
        self.assertEqual(data['plans_by_run_status'], {'running': 1})
        self.assertEqual(data['suites_published'], 1)
        self.assertEqual(data['cases_published'], 1)

    def test_data_freshness_block(self):
        IntradayPoint.objects.create(
            symbol=self.symbol, ts=self.recent, price=Decimal('10'), change=Decimal('1.2'))

        data = services.overview()['data_freshness']

        self.assertEqual(data['symbols'], 1)
        self.assertEqual(data['intraday_points'], 1)
        self.assertIsNotNone(data['intraday_last_at'])

    def test_alerts_open_count_is_full_table(self):
        for severity in ('high', 'high', 'low'):
            alert = Alert.objects.create(
                alert_type='suite_failed', severity=severity, title='t', message='m')
            Alert.objects.filter(pk=alert.pk).update(created_at=self.recent)
        Alert.objects.create(alert_type='suite_failed', severity='high',
                             title='t', message='m', status='resolved')

        data = services.overview(window_days=1)['alerts']

        self.assertEqual(data['open'], 3)                            # 全表未处理
        self.assertEqual(data['open_by_severity'], {'high': 2, 'low': 1})
        self.assertEqual(data['window_total'], 4)                   # 含已解决的 1 条


class IntentHealthConsistencyTest(TestCase):
    """总览数到的「过期意向」必须与恢复器实际会收口的一致（同一口径）。"""

    def setUp(self):
        self.plan, _ = _base_fixture()
        self.now = timezone.now()

    def test_expired_candidates_match_recovery_scope(self):
        expired = _make_run(self.plan, 'pending')
        SuiteRun.objects.filter(pk=expired.pk).update(
            created_at=self.now - timedelta(seconds=600))          # 超过 300s 有效期
        _make_run(self.plan, 'pending')                             # 新鲜，保留

        health = services.overview()['intents']
        stats = recover_orphaned_runs(now=self.now, pending_max_age=300, notify=False)

        self.assertEqual(health['pending'], 2)
        self.assertEqual(health['expired_candidates'], 1)
        self.assertEqual(stats['pending_expired'], health['expired_candidates'])

    def test_intent_ttl_is_reported_for_transparency(self):
        self.assertEqual(services.overview()['intents']['max_age_seconds'], 300)


class TrendTest(TestCase):
    """趋势：补零 + 日期连续 + 按市场时区分日。"""

    def setUp(self):
        self.plan, _ = _base_fixture()

    def test_missing_days_are_zero_filled(self):
        today = timezone.now()
        only_today = _make_run(self.plan, 'completed')
        SuiteRun.objects.filter(pk=only_today.pk).update(created_at=today)

        series = services.execution_trend(days=7)

        self.assertEqual(len(series), 7)
        self.assertEqual(sum(item['total'] for item in series), 1)
        self.assertEqual(series[-1]['total'], 1)                    # 今天在最后一位
        self.assertTrue(all(item['total'] == 0 for item in series[:-1]))

    def test_day_bucketing_uses_market_timezone_not_utc(self):
        """上海 00:30 = 前一日 UTC 16:30；按 UTC 分桶会归错一天。"""
        shanghai_day = timezone.now().astimezone(SHANGHAI).date()
        shanghai_early = datetime.combine(
            shanghai_day, dtime(0, 30), tzinfo=SHANGHAI).astimezone(dt_timezone.utc)
        run = _make_run(self.plan, 'completed')
        SuiteRun.objects.filter(pk=run.pk).update(created_at=shanghai_early)

        series = services.execution_trend(days=3)
        target = next(item for item in series if item['date'] == shanghai_day.isoformat())

        self.assertEqual(target['total'], 1)
        self.assertEqual(target['success_rate'], 100.0)
        self.assertEqual(sum(item['total'] for item in series), 1)
        utc_day = shanghai_early.astimezone(dt_timezone.utc).date()
        if utc_day != shanghai_day:
            # 两个时区落在不同自然日时，UTC 那一天必须是 0（否则说明按 UTC 分了桶）
            self.assertEqual(
                next(item for item in series if item['date'] == utc_day.isoformat())['total'], 0)

    def test_success_rate_is_none_when_no_settled_runs(self):
        _make_run(self.plan, 'pending')

        series = services.execution_trend(days=1)

        self.assertEqual(series[0]['total'], 1)
        self.assertIsNone(series[0]['success_rate'])                 # pending 不产生 0%


class NormalizeTest(TestCase):
    """类型归一：金额不丢精度、时间可安全序列化。"""

    def test_decimal_becomes_string_and_datetime_iso(self):
        payload = normalize({
            'amount': Decimal('1234.50'),
            'when': timezone.now(),
            'nested': [{'x': Decimal('1')}],
        })

        self.assertEqual(payload['amount'], '1234.50')
        self.assertIn('T', payload['when'])
        self.assertEqual(payload['nested'][0]['x'], '1')

    def test_plain_values_pass_through(self):
        self.assertEqual(normalize({'a': 1, 'b': None, 'c': 1.5}),
                         {'a': 1, 'b': None, 'c': 1.5})


class DashboardApiTest(APITestCase):
    """API 契约：鉴权、字段完整性、非法参数兜底、N-05 卫生。"""

    def setUp(self):
        self.staff = User.objects.create_user('dashadmin', password='x', is_staff=True)
        self.plain = User.objects.create_user('dashplain', password='x')
        self.plan, self.symbol = _base_fixture()

    def test_unauthenticated_is_rejected(self):
        for url in ('/api/dashboard/overview/', '/api/dashboard/execution-trend/'):
            resp = self.client.get(url)
            self.assertIn(resp.status_code, (401, 403), url)

    def test_ordinary_user_can_read_overview(self):
        self.client.force_authenticate(self.plain)

        resp = self.client.get('/api/dashboard/overview/')

        self.assertEqual(resp.status_code, 200)
        for block in ('execution', 'intents', 'orders', 'funds', 'alerts',
                      'config', 'data_freshness'):
            self.assertIn(block, resp.data)

    def test_trend_response_shape(self):
        self.client.force_authenticate(self.staff)

        resp = self.client.get('/api/dashboard/execution-trend/?days=3')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['days'], 3)
        self.assertEqual(len(resp.data['series']), 3)
        self.assertEqual(
            set(resp.data['series'][0]),
            {'date', 'total', 'completed', 'failed', 'stopped', 'settled', 'success_rate'})

    def test_invalid_days_parameter_falls_back_to_default(self):
        self.client.force_authenticate(self.staff)

        resp = self.client.get('/api/dashboard/overview/?window_days=abc')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['window_days'], services.DEFAULT_WINDOW_DAYS)

    def test_window_days_is_clamped(self):
        self.client.force_authenticate(self.staff)

        resp = self.client.get('/api/dashboard/overview/?window_days=99999')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['window_days'], 365)

    def test_response_never_exposes_account_id(self):
        """N-05 卫生：总览只给聚合数值，不回显账户 ID。"""
        # 注意：allocated_capital 是模型 property（按 Plan 聚合），不能作为字段写入
        AccountFundConfig.objects.create(
            account_id='SECRET-ACCOUNT-0001', total_capital=Decimal('1000'),
            source='gm', synced_at=timezone.now())
        self.plan.account_id = 'SECRET-ACCOUNT-0001'
        self.plan.allocated_capital = Decimal('100')
        self.plan.save(update_fields=['account_id', 'allocated_capital'])
        self.client.force_authenticate(self.staff)

        resp = self.client.get('/api/dashboard/overview/')

        self.assertNotIn('SECRET-ACCOUNT-0001', str(resp.data))
        self.assertTrue(resp.data['funds']['configured'])
        self.assertEqual(resp.data['funds']['total_capital'], '1000.00')
        self.assertEqual(resp.data['funds']['available_capital'], '900.00')


