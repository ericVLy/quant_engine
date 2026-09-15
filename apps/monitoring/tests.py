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
from unittest.mock import patch

from apps.datasources.models import RealtimeSnapshot
from apps.watchlists.models import Symbol

from .market_calendar import (
    MARKET_TIMEZONES, in_trading_session, session_status, to_market_local,
)
from .models import IntradayPoint
from .snapshot_provider import normalize_spot_frame
from .services import backfill_intraday, clear_intraday, sample_intraday
from .updater import IntradayUpdater


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

    def test_trading_minutes_local_morning(self):
        from .market_calendar import trading_minutes_local
        minutes = trading_minutes_local('A', self._at(self.MONDAY, 10, 5))
        self.assertEqual(minutes[0], self._at(self.MONDAY, 9, 30).replace(second=0, microsecond=0))
        self.assertEqual(minutes[-1], self._at(self.MONDAY, 10, 5))
        # 09:30→10:05 = 36 分钟，全部处于交易时段
        self.assertEqual(len(minutes), 36)
        self.assertTrue(all(30 <= m.hour * 60 + m.minute < 11 * 60 + 30 for m in minutes))

    def test_trading_minutes_local_lunch_break_excluded(self):
        from .market_calendar import trading_minutes_local
        minutes = trading_minutes_local('A', self._at(self.MONDAY, 13, 10))
        times = set(m.hour * 60 + m.minute for m in minutes)
        # 12:00（午休起点）不参与；午休时段内无分钟
        self.assertNotIn(12 * 60 + 0, times)
        self.assertNotIn(12 * 60 + 30, times)
        # 上午 + 下午 13:00→13:10 均包含
        self.assertIn(9 * 60 + 30, times)
        self.assertIn(13 * 60 + 10, times)

    def test_trading_minutes_local_empty_when_pre_market_or_weekend(self):
        from .market_calendar import trading_minutes_local
        self.assertEqual(trading_minutes_local('A', self._at(self.MONDAY, 9, 0)), [])
        self.assertEqual(trading_minutes_local('A', self._at(self.SATURDAY, 10, 0)), [])

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
def test_sample_passes_symbols_to_provider(self):
        class _RecordingProvider:
            def __init__(self):
                self.calls = []

            def fetch_market(self, market, symbols):
                self.calls.append((market, symbols))
                return {}

        provider = _RecordingProvider()
        now = self._aware_utc('A', 10, 0)
        sample_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(len(provider.calls), 1)
        market, symbol_list = provider.calls[0]
        self.assertEqual(market, 'A')
        self.assertEqual([s.code for s in symbol_list], ['000001'])


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
class GmSnapshotProviderTest(TestCase):
    def setUp(self):
        self.sse = Symbol.objects.create(
            code='600000', name='浦发银行', market='A', exchange='SSE',
        )
        self.szse = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE',
        )

    def test_gm_symbol_for_maps_sse_and_szse(self):
        from .snapshot_provider import gm_symbol_for
        self.assertEqual(gm_symbol_for(self.sse), 'SHSE.600000')
        self.assertEqual(gm_symbol_for(self.szse), 'SZSE.000001')

    def test_gm_symbol_for_distinguishes_index_and_stock(self):
        """指数与个股不混淆：指数专属段 + 000xxx 二义段以 exchange 为准。"""
        from .snapshot_provider import gm_symbol_for
        # 深市指数（指数专属段，纯前缀判定）
        sz_index = Symbol.objects.create(code='399001', name='深证成指', market='A', exchange='')
        self.assertEqual(gm_symbol_for(sz_index), 'SZSE.399001')
        # 沪指数：000xxx + SSE（上证指数）
        sh_index = Symbol.objects.create(code='sh000300', name='沪深300', market='A', exchange='SSE')
        self.assertEqual(gm_symbol_for(sh_index), 'SHSE.000300')
        # 二义段缺省：裸 000xxx 无 exchange → 深市个股（与历史缺省一致）
        self.assertEqual(gm_symbol_for(self.szse), 'SZSE.000001')
        # 中证系列指数段 → 沪市
        csi = Symbol.objects.create(code='930955', name='中证机器人', market='A', exchange='')
        self.assertEqual(gm_symbol_for(csi), 'SHSE.930955')
        # 北交所个股 → BJSE
        bse = Symbol.objects.create(code='833533', name='骏创科技', market='A', exchange='BSE')
        self.assertEqual(gm_symbol_for(bse), 'BJSE.833533')

    def test_gm_symbol_for_non_a_raises(self):
        from .snapshot_provider import gm_symbol_for
        hk = Symbol.objects.create(code='00700', name='腾讯', market='HK')
        with self.assertRaises(ValueError):
            gm_symbol_for(hk)

    def test_fetch_market_normalizes_tick_snapshot(self):
        from .snapshot_provider import GmSnapshotProvider

        class _Broker:
            def history(self, symbol, frequency, start_time, end_time):
                return [{
                    'symbol': symbol, 'price': 10.8, 'open': 10.3,
                    'high': 11.2, 'low': 10.1, 'cum_volume': 2000,
                    'cum_amount': 21600.0,
                }]

            def history_n(self, symbol, frequency, count, data_frame=False):
                # 前日 + 昨日 1d bar（不含今日：盘前/刚开盘场景），昨日 close 作 pre_close
                return [
                    {'symbol': symbol, 'close': 9.9, 'eob': '2026-09-11 15:00:00+08:00'},
                    {'symbol': symbol, 'close': 10.2, 'eob': '2026-09-13 15:00:00+08:00'},
                ]

        provider = GmSnapshotProvider(broker=_Broker())
        out = provider.fetch_market('A', [self.sse])
        self.assertIn('600000', out)
        item = out['600000']
        self.assertEqual(item['price'], 10.8)
        self.assertEqual(item['volume'], 2000)
        self.assertEqual(item['amount'], 21600.0)
        self.assertEqual(item['open_price'], 10.3)
        self.assertEqual(item['high'], 11.2)
        self.assertEqual(item['low'], 10.1)
        self.assertAlmostEqual(item['pre_close'], 10.2)
        # change = (10.8 - 10.2) / 10.2 * 100
        self.assertAlmostEqual(item['change'], round((10.8 - 10.2) / 10.2 * 100.0, 4))

    @patch('django.utils.timezone.now')
    def test_pre_close_skips_today_bar(self, mock_now):
        """含今日 bar 时昨收取**日期早于今日**的最近一根，而非 bars[-2]。

        固定“当前时间”避免用例变成时间炸弹：以 2026-09-14（周一，交易日）
        为今日，bars 中 09-14 为今日 bar，昨收应取 09-13 的 close=10.2。
        """
        from .snapshot_provider import GmSnapshotProvider

        mock_now.return_value = datetime(
            2026, 9, 14, 10, 0, 0, tzinfo=zoneinfo.ZoneInfo('Asia/Shanghai'),
        )

        class _Broker:
            def __init__(self):
                self.count = None

            def history(self, symbol, frequency, start_time, end_time):
                return [{
                    'symbol': symbol, 'time': '2026-09-14 09:31:00',
                    'open': 10.3, 'high': 10.5, 'low': 10.1, 'close': 10.4,
                    'volume': 100, 'amount': 1040.0,
                }]

            def history_n(self, symbol, frequency, count, data_frame=False):
                self.count = count
                return [
                    {'symbol': symbol, 'close': 9.8, 'eob': '2026-09-10 15:00:00+08:00'},
                    {'symbol': symbol, 'close': 10.2, 'eob': '2026-09-13 15:00:00+08:00'},
                    {'symbol': symbol, 'close': 10.5, 'eob': '2026-09-14 15:00:00+08:00'},  # 今日
                ]

        broker = _Broker()
        provider = GmSnapshotProvider(broker=broker)
        out = provider.fetch_market('A', [self.sse])
        item = out.get('600000')
        self.assertIsNotNone(item)
        self.assertAlmostEqual(item['pre_close'], 10.2)

    def test_fetch_market_uses_gm_symbol(self):
        from .snapshot_provider import GmSnapshotProvider

        class _Broker:
            def __init__(self):
                self.symbols = []

            def history(self, symbol, frequency, start_time, end_time):
                self.symbols.append(symbol)
                return [{'symbol': symbol, 'price': 10.0, 'cum_volume': 1}]

            def history_n(self, symbol, frequency, count, data_frame=False):
                return []

        broker = _Broker()
        provider = GmSnapshotProvider(broker=broker)
        out = provider.fetch_market('A', [self.sse, self.szse])
        self.assertEqual(set(out), {'600000', '000001'})
        self.assertEqual(set(broker.symbols), {'SHSE.600000', 'SZSE.000001'})

    def test_fetch_market_no_tick_returns_empty(self):
        from .snapshot_provider import GmSnapshotProvider

        class _Broker:
            def history(self, symbol, frequency, start_time, end_time):
                return []

            def history_n(self, symbol, frequency, count, data_frame=False):
                return []

        provider = GmSnapshotProvider(broker=_Broker())
        self.assertEqual(provider.fetch_market('A', [self.sse]), {})

    def test_fetch_market_isolates_symbol_failure(self):
        from .snapshot_provider import GmSnapshotProvider

        class _Broker:
            def history(self, symbol, frequency, start_time, end_time):
                raise RuntimeError('gm 终端不可用')

            def history_n(self, symbol, frequency, count, data_frame=False):
                return []

        provider = GmSnapshotProvider(broker=_Broker())
        # 单个标的失败不影响其他标的，且不向上抛
        self.assertEqual(provider.fetch_market('A', [self.sse, self.szse]), {})

    def test_fetch_market_non_a_raises(self):
        from .snapshot_provider import GmSnapshotProvider
        provider = GmSnapshotProvider(broker=object())
        with self.assertRaises(ValueError):
            provider.fetch_market('HK', [self.sse])

    def test_fetch_intraday_history_returns_minute_bars(self):
        from .snapshot_provider import GmSnapshotProvider

        class _Broker:
            def __init__(self):
                self.calls = []

            def history(self, symbol, frequency, start_time, end_time):
                self.calls.append((symbol, frequency))
                return [{'symbol': symbol, 'time': '2026-09-14 09:30:00', 'open': 10.0, 'high': 10.5, 'low': 9.9, 'close': 10.2, 'volume': 100, 'amount': 1020.0}]

            def history_n(self, symbol, frequency, count, data_frame=False):
                return [{'close': 10.0}]

        broker = _Broker()
        provider = GmSnapshotProvider(broker=broker)
        utc = zoneinfo.ZoneInfo('UTC')
        start = datetime(2026, 9, 14, 1, 29, tzinfo=utc)
        end = datetime(2026, 9, 14, 1, 31, tzinfo=utc)
        bars = provider.fetch_intraday_history('A', self.sse, start, end)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]['ts'], datetime(2026, 9, 14, 1, 30, tzinfo=utc))
        self.assertEqual(bars[0]['close'], 10.2)
        self.assertEqual(bars[0]['volume'], 100)
        self.assertEqual(bars[0]['pre_close'], 10.0)
        self.assertEqual(broker.calls[0][0], 'SHSE.600000')
        self.assertEqual(broker.calls[0][1], '60s')

class CompositeSnapshotProviderTest(TestCase):
    def setUp(self):
        self.sse = Symbol.objects.create(
            code='600000', name='浦发银行', market='A', exchange='SSE',
        )

    def test_uses_primary_when_supported(self):
        from .snapshot_provider import CompositeSnapshotProvider

        class _Primary:
            def fetch_market(self, market, symbols=None):
                return {'600000': {'price': 10.5, 'change': 1.0, 'volume': 100}}

        class _Fallback:
            def fetch_market(self, market, symbols=None):
                raise AssertionError('不应触发')

        provider = CompositeSnapshotProvider([_Primary(), _Fallback()])
        out = provider.fetch_market('A', [self.sse])
        self.assertEqual(out['600000']['price'], 10.5)

    def test_falls_back_when_primary_unsupported(self):
        from .snapshot_provider import CompositeSnapshotProvider

        class _Unsupported:
            def fetch_market(self, market, symbols=None):
                raise ValueError(f'gm SDK 不支持的市场: {market}')

        class _Akshare:
            def fetch_market(self, market, symbols=None):
                return {'600000': {'price': 20.0, 'change': -0.5, 'volume': 200}}

        provider = CompositeSnapshotProvider([_Unsupported(), _Akshare()])
        out = provider.fetch_market('A', [self.sse])
        self.assertEqual(out['600000']['price'], 20.0)

    def test_falls_back_when_primary_returns_empty(self):
        from .snapshot_provider import CompositeSnapshotProvider

        class _Empty:
            def fetch_market(self, market, symbols=None):
                return {}

        class _Filled:
            def fetch_market(self, market, symbols=None):
                return {'600000': {'price': 9.9}}

        provider = CompositeSnapshotProvider([_Empty(), _Filled()])
        self.assertEqual(provider.fetch_market('A', [self.sse])['600000']['price'], 9.9)

    def test_all_providers_fail_raises(self):
        from .snapshot_provider import CompositeSnapshotProvider

        class _Fail:
            def fetch_market(self, market, symbols=None):
                raise RuntimeError('数据源不可用')

        provider = CompositeSnapshotProvider([_Fail(), _Fail()])
        with self.assertRaises(RuntimeError):
            provider.fetch_market('A', [self.sse])
    def test_forwards_intraday_history_to_first_success(self):
        from .snapshot_provider import CompositeSnapshotProvider
        utc = zoneinfo.ZoneInfo('UTC')

        class _One:
            def fetch_intraday_history(self, market, symbol, start, end):
                return [{'ts': start, 'close': 11.0}]

        class _Two:
            def fetch_intraday_history(self, *args, **kwargs):
                raise AssertionError('不应触发')

        provider = CompositeSnapshotProvider([_One(), _Two()])
        start = datetime(2026, 9, 14, 1, 30, tzinfo=utc)
        end = datetime(2026, 9, 14, 1, 31, tzinfo=utc)
        bars = provider.fetch_intraday_history('A', self.sse, start, end)
        self.assertEqual(bars[0]['close'], 11.0)

    def test_falls_back_on_history_error(self):
        from .snapshot_provider import CompositeSnapshotProvider
        utc = zoneinfo.ZoneInfo('UTC')

        class _Err:
            def fetch_intraday_history(self, *args, **kwargs):
                raise ValueError('gm SDK 不支持的市场')

        class _Ok:
            def fetch_intraday_history(self, market, symbol, start, end):
                return [{'close': 9.9}]

        provider = CompositeSnapshotProvider([_Err(), _Ok()])
        start = datetime(2026, 9, 14, 1, 30, tzinfo=utc)
        end = datetime(2026, 9, 14, 1, 31, tzinfo=utc)
        self.assertEqual(
            provider.fetch_intraday_history('A', self.sse, start, end)[0]['close'], 9.9,
        )
class BackfillIntradayTest(TestCase):
    """启动完整性回填专项测试（2026-09-14 周一为参照）。"""

    def setUp(self):
        self.a1 = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE',
        )

    @staticmethod
    def _utc(hour, minute):
        local = datetime(2026, 9, 14, hour, minute, tzinfo=zoneinfo.ZoneInfo('Asia/Shanghai'))
        return local.astimezone(zoneinfo.ZoneInfo('UTC')).replace(second=0, microsecond=0)

    @staticmethod
    def _bar(minute, idx, pre_close=10.0):
        """分钟 bar：high 单调上升便于断言日内最高；volume=idx。"""
        return {
            'ts': minute,
            'open': 10.0,
            'high': 10.0 + idx * 0.1,
            'low': 10.0,
            'close': 10.0,
            'volume': idx + 1,
            'amount': (idx + 1) * 10.0,
            'pre_close': pre_close,
        }

    def test_backfill_fills_missing_minutes_with_snapshot_semantics(self):
        start = self._utc(9, 30)
        now = self._utc(10, 5)
        provider = type('P', (), {'fetch_intraday_history': lambda s, m, sym, st, en: [
            self._bar(start + timedelta(minutes=i), i) for i in range(35)
        ]})()
        summary = backfill_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(summary['A']['backfilled'], 35)
        points = IntradayPoint.objects.filter(symbol=self.a1).order_by('ts')
        self.assertEqual(points.count(), 35)
        last = points.last()
        # 累计成交量 = 1+2+...+35（10:05 为进行中的分钟，bar 未生成，不计入目标）
        self.assertEqual(last.volume, sum(range(1, 36)))
        # 日内最高 = 10.0 + 34*0.1
        self.assertEqual(last.high, Decimal('13.4'))
        self.assertEqual(last.open_price, Decimal('10.0'))
        self.assertEqual(last.price, Decimal('10.0'))

    def test_backfill_does_not_overwrite_existing_point(self):
        start = self._utc(9, 30)
        now = self._utc(10, 5)
        IntradayPoint.objects.create(
            symbol=self.a1, ts=start, price='99.00', change='0', volume=5,
        )
        provider = type('P', (), {'fetch_intraday_history': lambda s, m, sym, st, en: [
            self._bar(start + timedelta(minutes=i), i) for i in range(35)
        ]})()
        summary = backfill_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(summary['A']['backfilled'], 34)  # 09:30 已存在，不覆盖；10:05 进行中不计
        kept = IntradayPoint.objects.get(symbol=self.a1, ts=start)
        self.assertEqual(kept.price, Decimal('99.00'))

    def test_backfill_skips_when_complete(self):
        start = self._utc(9, 30)
        now = self._utc(10, 5)
        for i in range(36):
            IntradayPoint.objects.create(
                symbol=self.a1, ts=start + timedelta(minutes=i),
                price='10.0', change='0', volume=i + 1,
            )
        provider = type('P', (), {
            'fetch_intraday_history': lambda s, m, sym, st, en: self.fail('完整时不应拉取历史'),
        })()
        summary = backfill_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(summary['A']['checked'], 1)
        self.assertEqual(summary['A']['complete'], 1)
        self.assertEqual(summary['A']['backfilled'], 0)

    def test_backfill_skips_unsupported_history(self):
        now = self._utc(10, 5)
        provider = type('P', (), {
            'fetch_intraday_history': lambda s, m, sym, st, en: (_ for _ in ()).throw(
                NotImplementedError('不支持历史'),
            ),
        })()
        summary = backfill_intraday(provider=provider, symbols=['000001'], now=now)
        self.assertEqual(summary['A']['backfilled'], 0)
        self.assertEqual(IntradayPoint.objects.count(), 0)

    def test_backfill_covers_both_a_sessions(self):
        """13:10 时：上午 09:30-11:29（120） + 下午 13:00-13:09（10）= 130 分钟（13:10 进行中不计）。"""
        morning = [self._utc(9, 30) + timedelta(minutes=i) for i in range(120)]
        afternoon = [self._utc(13, 0) + timedelta(minutes=i) for i in range(10)]
        bars = [self._bar(m, i) for i, m in enumerate(morning + afternoon)]
        provider = type('P', (), {'fetch_intraday_history': lambda s, m, sym, st, en: bars})()
        summary = backfill_intraday(provider=provider, symbols=['000001'], now=self._utc(13, 10))
        self.assertEqual(summary['A']['backfilled'], 130)
        self.assertEqual(IntradayPoint.objects.filter(symbol=self.a1).count(), 130)


class IntradayUpdaterTest(TestCase):
    """内部更新器（随 Django 服务启动，替代独立更新命令）。"""

    def setUp(self):
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.updater = IntradayUpdater(interval=1)

    def test_run_once_delegates_to_sample_intraday(self):
        import apps.monitoring.updater as updater_module

        provider = _FakeSpotProvider(quotes={'A': {'000001': {'price': 10.5}}})
        calls = []

        def fake_sample(provider=None, markets=None, symbols=None, now=None):
            calls.append(provider)
            return {'A': {'sampled': 1, 'skipped': 0, 'failed': []}}

        original = updater_module.sample_intraday
        updater_module.sample_intraday = fake_sample
        try:
            summary = self.updater.run_once(provider=provider)
        finally:
            updater_module.sample_intraday = original
        self.assertEqual(summary['A']['sampled'], 1)
        self.assertEqual(calls, [provider])

    def test_cleanup_triggers_once_per_utc_day(self):
        import apps.monitoring.updater as updater_module

        deleted_log = []
        original_clear = updater_module.clear_intraday
        updater_module.clear_intraday = lambda before=None, now=None: deleted_log.append(now) or 5
        try:
            late = datetime(2026, 9, 14, 23, 5, tzinfo=zoneinfo.ZoneInfo('UTC'))
            self.updater._maybe_cleanup(now=late)
            self.assertEqual(len(deleted_log), 1)
            # 同一天重复调用不触发（幂等）
            self.updater._maybe_cleanup(now=late)
            self.assertEqual(len(deleted_log), 1)
            # 次日 UTC 23:00 再次触发
            nxt = datetime(2026, 9, 15, 23, 0, tzinfo=zoneinfo.ZoneInfo('UTC'))
            self.updater._maybe_cleanup(now=nxt)
            self.assertEqual(len(deleted_log), 2)
            # 非 23 点不触发
            self.updater._last_cleanup_date = None
            noon = datetime(2026, 9, 16, 12, 0, tzinfo=zoneinfo.ZoneInfo('UTC'))
            self.updater._maybe_cleanup(now=noon)
            self.assertEqual(len(deleted_log), 2)
        finally:
            updater_module.clear_intraday = original_clear

    def test_backfill_retries_until_complete(self):
        """启动回填失败/不完整时每轮重试，完整后置位并跳过。"""
        import apps.monitoring.updater as updater_module

        summaries = [
            {'A': {'checked': 1, 'complete': 0, 'backfilled': 0, 'missing_remaining': ['09:31']}},
            {'A': {'checked': 1, 'complete': 1, 'backfilled': 1, 'missing_remaining': []}},
        ]
        calls = []

        def fake_backfill(**kwargs):
            calls.append(1)
            return summaries.pop(0)

        original = updater_module.backfill_intraday
        updater_module.backfill_intraday = fake_backfill
        try:
            self.updater._maybe_backfill()
            self.assertFalse(self.updater._backfill_complete)
            # 不完整 → 下一轮重试
            self.updater._maybe_backfill()
            self.assertTrue(self.updater._backfill_complete)
            # 完整后跳过，不再调用
            self.updater._maybe_backfill()
            self.assertEqual(len(calls), 2)
        finally:
            updater_module.backfill_intraday = original

    def test_backfill_exception_does_not_lose_retry(self):
        """启动回填抛异常时置位不变，下一轮仍会重试。"""
        import apps.monitoring.updater as updater_module

        def broken_backfill(**kwargs):
            raise RuntimeError('gm not ready')

        original = updater_module.backfill_intraday
        updater_module.backfill_intraday = broken_backfill
        try:
            self.updater._maybe_backfill()
            self.assertFalse(self.updater._backfill_complete)
        finally:
            updater_module.backfill_intraday = original

    def test_get_updater_returns_singleton(self):
        from .updater import get_updater

        self.assertIs(get_updater(), get_updater())

    def test_start_is_idempotent(self):
        # 不真正运行线程循环：start 后立即 stop，重复 start 不叠加线程
        try:
            thread = self.updater.start()
            self.assertTrue(self.updater.is_running)
            again = self.updater.start()
            self.assertIs(thread, again)
        finally:
            self.updater.stop(timeout=5)
            self.assertFalse(self.updater.is_running)


class SseStreamTest(APITestCase):
    """``GET /api/monitoring/intraday/stream/``（SSE 持久化推送）。"""

    def setUp(self):
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.ts = timezone.now().replace(second=0, microsecond=0)
        IntradayPoint.objects.create(
            symbol=self.symbol, ts=self.ts, price='10.50', change='1.20', volume=1000,
        )
        self.url = '/api/monitoring/intraday/stream/'

    def test_stream_first_chunk_is_snapshot_event(self):
        # HTTP_ACCEPT 模拟 EventSource 实际发送的 Accept: text/event-stream
        response = self.client.get(self.url, {'symbol': '000001'}, HTTP_ACCEPT='text/event-stream')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        self.assertTrue(response.streaming)
        first = next(response.streaming_content).decode('utf-8')
        response.close()
        self.assertTrue(first.startswith('event: snapshot\n'))
        self.assertIn('"symbol": "000001"', first)
        self.assertIn('"local_time"', first)

    def test_stream_accepts_drf_json_accept_header(self):
        # 以前挂 DRF ViewSet 时此场景返回 406；普通视图下不应再内容协商
        response = self.client.get(self.url, {'symbol': '000001'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        response.close()

    def test_stream_requires_symbol(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)



class AkshareIndexFallbackTest(TestCase):
    """akshare 回退链路的指数支持：个股 spot 不含指数，按需合并指数 spot。"""

    def _make_provider(self, index_df):
        from .snapshot_provider import AkshareSpotProvider

        class _FakeAk:
            @staticmethod
            def stock_zh_a_spot_em():
                import pandas as pd
                return pd.DataFrame([{'代码': '000426', '名称': '兴业银锡', '最新价': 10.0}])

            @staticmethod
            def stock_zh_index_spot_sina():
                return index_df

        return AkshareSpotProvider(ak_module=_FakeAk)

    def test_index_codes_merged_when_requested(self):
        import pandas as pd
        index_df = pd.DataFrame([
            {'代码': 'sh000300', '名称': '沪深300', '最新价': 4000.5, '涨跌幅': 0.5,
             '成交量': 100, '成交额': 200, '最高': 4010, '最低': 3990, '昨收': 3980.2, '今开': 3995.0},
            {'代码': 'sz399001', '名称': '深证成指', '最新价': 13000.0, '涨跌幅': -0.2,
             '成交量': 100, '成交额': 200, '最高': 13010, '最低': 12990, '昨收': 13030, '今开': 13020},
        ])
        provider = self._make_provider(index_df)
        sh300 = Symbol.objects.create(code='000300', name='沪深300', market='A', exchange='SSE')
        stock = Symbol.objects.create(code='000426', name='兴业银锡', market='A', exchange='SZSE')
        out = provider.fetch_market('A', [sh300, stock])
        # 指数经 sina 前缀剥除后按 6 位代码合并
        self.assertIn('000300', out)
        self.assertEqual(out['000300']['price'], 4000.5)
        self.assertEqual(out['000300']['pre_close'], 3980.2)
        self.assertIn('000426', out)
        # 未请求的指数不合并
        self.assertNotIn('399001', out)

    def test_no_index_requested_skips_index_spot(self):
        called = {'index': False}

        class _FakeAk:
            @staticmethod
            def stock_zh_a_spot_em():
                import pandas as pd
                return pd.DataFrame([{'代码': '000426', '名称': '兴业银锡', '最新价': 10.0}])

            @staticmethod
            def stock_zh_index_spot_sina():
                called['index'] = True
                import pandas as pd
                return pd.DataFrame()

        from .snapshot_provider import AkshareSpotProvider
        provider = AkshareSpotProvider(ak_module=_FakeAk)
        stock = Symbol.objects.create(code='000426', name='兴业银锡', market='A', exchange='SZSE')
        out = provider.fetch_market('A', [stock])
        self.assertIn('000426', out)
        self.assertFalse(called['index'])  # 全个股请求不触发指数接口

    def test_ambiguous_000_without_exchange_is_stock(self):
        # 000xxx 无 exchange → 个股缺省，不进指数合并清单
        import pandas as pd
        provider = self._make_provider(pd.DataFrame())
        stock = Symbol.objects.create(code='000001', name='平安银行', market='A', exchange='')
        out = provider.fetch_market('A', [stock])
        self.assertIn('000426', out)
        self.assertEqual(out['000426']['price'], 10.0)
