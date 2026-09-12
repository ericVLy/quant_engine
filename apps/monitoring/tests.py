"""分时监控（模块9）专项测试。

对齐 documents.md 5.2 P1 阶段4 验收清单：
- ``IntradayPoint`` 模型 / (symbol, ts) 唯一约束
- ``market_calendar``：多市场交易时段 / 午休 / 开盘前 / 收盘 / 周末 / 美股夏令时
- ``snapshot_provider``：spot DataFrame 规范化（列别名 / 缺失字段降级）
- ``sample_intraday``：采样写入 / 同分钟覆盖 / 非交易时段跳过 / 市场过滤 / provider 异常隔离
- ``clear_intraday``：默认当日 00:00 UTC 边界 / 幂等
- API：/api/monitoring/intraday/ 响应契约 + realtime 合并 RealtimeSnapshot
"""
import zoneinfo
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.datasources.models import RealtimeSnapshot
from apps.watchlists.models import Symbol

from .market_calendar import (
    MARKET_TIMEZONES, in_trading_session, session_status, to_market_local,
)
from .models import IntradayPoint
from .snapshot_provider import normalize_spot_frame
from .services import clear_intraday, sample_intraday


class IntradayPointModelTest(TestCase):
    def setUp(self):
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.ts = timezone.now().replace(second=0, microsecond=0)

    def test_unique_symbol_ts(self):
        IntradayPoint.objects.create(
            symbol=self.symbol, ts=self.ts, price='10.50', change='1.20', volume=1000,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                IntradayPoint.objects.create(
                    symbol=self.symbol, ts=self.ts, price='10.60', change='1.30', volume=1100,
                )

    def test_same_symbol_different_minute_allowed(self):
        IntradayPoint.objects.create(
            symbol=self.symbol, ts=self.ts, price='10.50', change='1.20', volume=1000,
        )
        IntradayPoint.objects.create(
            symbol=self.symbol, ts=self.ts + timedelta(minutes=1),
            price='10.60', change='1.30', volume=1100,
        )
        self.assertEqual(IntradayPoint.objects.count(), 2)

    def test_str(self):
        point = IntradayPoint.objects.create(
            symbol=self.symbol, ts=self.ts, price='10.50', change='1.20', volume=1000,
        )
        self.assertIn('000001', str(point))


class MarketCalendarTest(TestCase):
    """以 2026-09-14（周一）与 2026-09-12（周六）为固定参照。"""

    MONDAY = datetime(2026, 9, 14)
    SATURDAY = datetime(2026, 9, 12)

    @staticmethod
    def _at(base, hour, minute):
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def test_a_session_status(self):
        self.assertEqual(session_status('A', self._at(self.MONDAY, 10, 0)), 'trading')
        self.assertEqual(session_status('A', self._at(self.MONDAY, 11, 29)), 'trading')
        self.assertEqual(session_status('A', self._at(self.MONDAY, 12, 0)), 'lunch_break')
        self.assertEqual(session_status('A', self._at(self.MONDAY, 13, 0)), 'trading')
        self.assertEqual(session_status('A', self._at(self.MONDAY, 9, 0)), 'pre_market')
        self.assertEqual(session_status('A', self._at(self.MONDAY, 15, 30)), 'closed')

    def test_in_trading_session_boundaries(self):
        self.assertTrue(in_trading_session('A', self._at(self.MONDAY, 9, 30)))
        self.assertFalse(in_trading_session('A', self._at(self.MONDAY, 11, 30)))
        self.assertTrue(in_trading_session('HK', self._at(self.MONDAY, 11, 59)))
        self.assertFalse(in_trading_session('HK', self._at(self.MONDAY, 12, 0)))
        self.assertTrue(in_trading_session('US', self._at(self.MONDAY, 15, 30)))
        self.assertFalse(in_trading_session('US', self._at(self.MONDAY, 16, 0)))

    def test_weekend_closed(self):
        self.assertEqual(session_status('A', self._at(self.SATURDAY, 10, 0)), 'closed')

    def test_to_market_local_utc_conversion(self):
        # 10:00 Asia/Shanghai == 02:00 UTC（非夏令时差 8h）
        utc_now = self._at(self.MONDAY, 2, 0).replace(tzinfo=zoneinfo.ZoneInfo('UTC'))
        self.assertEqual(to_market_local(utc_now, 'A'), self._at(self.MONDAY, 10, 0))

    def test_dst_us_market(self):
        # 美股：1 月 EST（UTC-5）、7 月 EDT（UTC-4）均为交易时段
        jan = datetime(2026, 1, 15, 10, 0, tzinfo=zoneinfo.ZoneInfo('America/New_York'))
        jul = datetime(2026, 7, 15, 10, 0, tzinfo=zoneinfo.ZoneInfo('America/New_York'))
        self.assertEqual(session_status('US', jan), 'trading')
        self.assertEqual(session_status('US', jul), 'trading')
        # 换算回 UTC 验证夏令时差异被感知
        self.assertEqual(jan.astimezone(zoneinfo.ZoneInfo('UTC')).hour, 15)  # EST = UTC-5
        self.assertEqual(jul.astimezone(zoneinfo.ZoneInfo('UTC')).hour, 14)  # EDT = UTC-4


class SnapshotProviderTest(TestCase):
    def test_normalize_spot_frame_aliases(self):
        import pandas as pd

        df = pd.DataFrame([
            {
                '代码': '000001', '名称': '平安银行', '最新价': 10.5, '涨跌幅': 1.2,
                '成交量': 1000, '成交金额': 10500.0, '最高': 10.8, '最低': 10.2,
                '开盘': 10.3, '昨收': 10.4,
            },
        ])
        out = normalize_spot_frame(df, 'A')
        self.assertIn('000001', out)
        item = out['000001']
        self.assertEqual(item['price'], 10.5)
        self.assertEqual(item['change'], 1.2)
        self.assertEqual(item['volume'], 1000)
        self.assertEqual(item['pre_close'], 10.4)

    def test_normalize_spot_frame_missing_field_degraded(self):
        import pandas as pd

        df = pd.DataFrame([{'代码': '000001', '最新价': 10.5}])
        item = normalize_spot_frame(df, 'A')['000001']
        self.assertIsNone(item['high'])
        self.assertIsNone(item['change'])

    def test_normalize_spot_frame_no_code_column_returns_empty(self):
        import pandas as pd

        df = pd.DataFrame([{'foo': 1, '最新价': 10.5}])
        self.assertEqual(normalize_spot_frame(df, 'A'), {})

    def test_normalize_spot_frame_drops_bad_rows(self):
        import pandas as pd

        df = pd.DataFrame([
            {'代码': '000001', '最新价': 10.5},
            {'代码': None, '最新价': 9.9},
            {'代码': 'AAPL', '最新价': 55.0},
        ])
        out = normalize_spot_frame(df, 'A')
        self.assertEqual(set(out.keys()), {'000001', 'AAPL'})


class _FakeSpotProvider:
    """测试用 mock provider：按市场返回预设快照，或按市场抛异常。"""

    def __init__(self, quotes=None, fail_markets=None):
        self.quotes = quotes or {}
        self.fail_markets = fail_markets or set()

    def fetch_market(self, market, symbols=None):
        if market in self.fail_markets:
            raise RuntimeError(f'{market} 快照拉取失败')
        return self.quotes.get(market, {})


class SampleIntradayTest(TestCase):
    def setUp(self):
        self.a1 = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.a2 = Symbol.objects.create(code='600000', name='浦发银行', market='A')
        self.hk = Symbol.objects.create(code='00700', name='腾讯', market='HK')
        self.us = Symbol.objects.create(code='AAPL', name='苹果', market='US')

    @staticmethod
    def _aware_utc(market_local, hour, minute):
        """把市场本地时间转为对应的 aware UTC now（周一 2026-09-14）。"""
        tz = zoneinfo.ZoneInfo(MARKET_TIMEZONES[market_local])
        local = datetime(2026, 9, 14, hour, minute, tzinfo=tz)
        return local.astimezone(zoneinfo.ZoneInfo('UTC'))

    def test_sample_writes_points_only_for_trading_markets(self):
        provider = _FakeSpotProvider(quotes={
            'A': {
                '000001': {'price': 10.5, 'change': 1.0, 'volume': 100},
                '600000': {'price': 20.0, 'change': -0.5, 'volume': 200},
            },
            'HK': {'00700': {'price': 300.0, 'change': 2.0, 'volume': 500}},
        })
        now = self._aware_utc('A', 10, 0)  # 上海 10:00 / HK 10:00 均在交易时段
        summary = sample_intraday(
            provider=provider, markets=['A', 'HK'],
            symbols=['000001', '600000', '00700'], now=now,
        )
        self.assertEqual(summary['A']['sampled'], 2)
        self.assertEqual(summary['HK']['sampled'], 1)

        expected_ts = now.replace(second=0, microsecond=0)
        self.assertEqual(IntradayPoint.objects.count(), 3)
        self.assertTrue(
            IntradayPoint.objects.filter(
                symbol=self.a1, ts=expected_ts, price='10.5000',
            ).exists()
        )
        self.assertEqual(
            IntradayPoint.objects.get(symbol=self.a2, ts=expected_ts).change,
            Decimal('-0.5000'),
        )

    def test_sample_same_minute_overwrites_via_update_or_create(self):
        provider = _FakeSpotProvider(quotes={
            'A': {'000001': {'price': 10.5, 'change': 1.0, 'volume': 100}},
        })
        now = self._aware_utc('A', 10, 0)
        sample_intraday(provider=provider, symbols=['000001'], now=now)
        # 同一 provider 同一分钟再次采样（最新价变化）→ 覆盖而非新增
        provider.quotes['A']['000001']['price'] = 10.8
        sample_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(IntradayPoint.objects.count(), 1)
        point = IntradayPoint.objects.get(symbol=self.a1)
        self.assertEqual(point.price, Decimal('10.8000'))

    def test_sample_skips_non_trading_market(self):
        provider = _FakeSpotProvider(quotes={
            'A': {'000001': {'price': 10.5, 'change': 1.0, 'volume': 100}},
        })
        now = self._aware_utc('A', 9, 0)  # 上海 09:00（开盘前）→ 跳过
        summary = sample_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(summary['A']['skipped'], 1)
        self.assertEqual(summary['A']['sampled'], 0)
        self.assertEqual(IntradayPoint.objects.count(), 0)

    def test_sample_provider_failure_isolated_per_market(self):
        provider = _FakeSpotProvider(
            quotes={'A': {'000001': {'price': 10.5, 'change': 1.0, 'volume': 100}}},
            fail_markets={'HK'},
        )
        now = self._aware_utc('A', 10, 0)
        summary = sample_intraday(
            provider=provider, markets=['A', 'HK'],
            symbols=['000001', '00700'], now=now,
        )
        self.assertEqual(summary['A']['sampled'], 1)
        self.assertEqual(len(summary['HK']['failed']), 1)
        self.assertEqual(IntradayPoint.objects.count(), 1)

    def test_sample_ignores_unknown_returned_codes(self):
        provider = _FakeSpotProvider(quotes={
            'A': {
                '000001': {'price': 10.5, 'change': 1.0, 'volume': 100},
                '000099': {'price': 99.0, 'change': 0.0, 'volume': 1},  # 不在库中
            },
        })
        now = self._aware_utc('A', 10, 0)
        sample_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(IntradayPoint.objects.count(), 1)


class ClearIntradayTest(TestCase):
    def setUp(self):
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        today = timezone.now().date()
        self.yesterday = timezone.make_aware(
            datetime.combine(today - timedelta(days=1), time(12, 0)),
            zoneinfo.ZoneInfo('UTC'),
        )
        self.today_early = timezone.make_aware(
            datetime.combine(today, time(6, 0)),
            zoneinfo.ZoneInfo('UTC'),
        )

    def _point(self, ts, price='10.50'):
        return IntradayPoint.objects.create(
            symbol=self.symbol, ts=ts, price=price, change='1.0', volume=100,
        )

    def test_default_before_today_utc_midnight(self):
        self._point(self.yesterday)
        self._point(self.today_early)
        deleted = clear_intraday()
        self.assertEqual(deleted, 1)
        self.assertEqual(IntradayPoint.objects.count(), 1)

    def test_idempotent(self):
        self._point(self.yesterday)
        self.assertEqual(clear_intraday(), 1)
        self.assertEqual(clear_intraday(), 0)

    def test_explicit_before(self):
        earlier = self.yesterday - timedelta(days=1)
        self._point(earlier)
        self._point(self.yesterday)
        deleted = clear_intraday(before=self.yesterday)
        self.assertEqual(deleted, 1)


class IntradayAPITest(APITestCase):
    def setUp(self):
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        base = datetime(2026, 9, 14, 2, 10, tzinfo=zoneinfo.ZoneInfo('UTC'))  # 上海 10:10
        self.p1 = IntradayPoint.objects.create(
            symbol=self.symbol, ts=base,
            price='10.50', change='1.20', volume=1000, amount='10500.00',
        )
        self.p2 = IntradayPoint.objects.create(
            symbol=self.symbol, ts=base + timedelta(minutes=1),
            price='10.60', change='1.30', volume=1100, amount='11160.00',
        )

    def test_intraday_series_contract(self):
        response = self.client.get('/api/monitoring/intraday/', {'symbol': '000001'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data['symbol'], '000001')
        self.assertEqual(data['market'], 'A')
        self.assertEqual(data['timezone'], 'Asia/Shanghai')
        self.assertIn(
            data['session_status'], ('trading', 'lunch_break', 'pre_market', 'closed'),
        )
        self.assertEqual(len(data['points']), 2)
        # 时间升序 + ts 为 UTC Z 格式 + local_time 按市场本地渲染
        first, second = data['points']
        self.assertLess(first['ts'], second['ts'])
        self.assertTrue(first['ts'].endswith('Z'))
        self.assertEqual(first['local_time'], '10:10')
        self.assertEqual(first['price'], '10.5000')
        self.assertEqual(first['volume'], 1000)

    def test_intraday_missing_symbol_400(self):
        response = self.client.get('/api/monitoring/intraday/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_intraday_unknown_symbol_404(self):
        response = self.client.get('/api/monitoring/intraday/', {'symbol': '999999'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_intraday_returns_plain_payload_not_paginated(self):
        response = self.client.get('/api/monitoring/intraday/', {'symbol': '000001'})
        self.assertNotIn('results', response.data)
        self.assertNotIn('count', response.data)

    def test_realtime_merges_latest_point_and_snapshot(self):
        RealtimeSnapshot.objects.create(
            symbol=self.symbol, price='10.60', change='1.30', volume=1100,
            turnover='11160.00', high='10.80', low='10.20',
            open_price='10.40', pre_close='10.20',
        )
        response = self.client.get('/api/monitoring/intraday/realtime/', {'symbol': '000001'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        # 仅最新一条
        self.assertEqual(len(data['points']), 1)
        self.assertEqual(data['points'][0]['local_time'], '10:11')
        # RealtimeSnapshot 合并
        self.assertEqual(data['pre_close'], '10.2000')
        self.assertEqual(data['realtime']['price'], '10.6000')
        self.assertEqual(data['realtime']['turnover'], '11160.00')
        self.assertEqual(data['realtime']['pre_close'], '10.2000')

    def test_realtime_without_snapshot_returns_null_realtime(self):
        response = self.client.get('/api/monitoring/intraday/realtime/', {'symbol': '000001'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['realtime'])
        self.assertEqual(response.data['pre_close'], None)