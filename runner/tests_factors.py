"""真实因子计算引擎与数据上下文构建测试。

覆盖 R-06（因子计算引擎）与 R-07（数据上下文）两大待办项。
"""

from datetime import date

from django.test import SimpleTestCase, TestCase

from apps.datasources.models import RealtimeSnapshot
from apps.watchlists.models import Symbol

from . import indicators as ind
from .factors import calculate, values_from
from .fixture import DataContextBuilder


def kline_context(count=60, base=50, step=1):
    """构造单调上涨的 K 线上下文，便于方向判定。"""
    return {'market_data': [
        {'date': date(2026, 1, 1 + i).isoformat() if i < 30 else None,
         'open': base + i * step, 'high': base + i * step + 0.5,
         'low': base + i * step - 0.5, 'close': base + i * step,
         'volume': 1000 + i, 'amount': 100000 + i}
        for i in range(count)
    ]}


def fallback_context():
    """仅提供最近价格的上下文，用于退化场景。"""
    return {'price': 120.0, 'last_close': 120.0, 'market_data': []}


class IndicatorTest(SimpleTestCase):
    def test_moving_average_last_value(self):
        ma = ind.moving_average([1, 2, 3, 4, 5], 5)
        self.assertIsNone(ma[0])
        self.assertEqual(ma[-1], 3.0)

    def test_rsi_reaches_100_for_strict_uptrend(self):
        rsi = ind.rsi(list(range(1, 21)), 14)
        self.assertEqual(rsi[-1], 100.0)

    def test_macd_returns_triple_same_length_series(self):
        dif, dea, hist = ind.macd([float(i) for i in range(1, 40)], 12, 26, 9)
        self.assertEqual(len(dif), 39)
        self.assertEqual(len(dea), 39)
        self.assertEqual(len(hist), 39)
        self.assertIsNotNone(hist[-1])

    def test_cross_detection(self):
        fast = [1, 1, 2, 4]
        slow = [2, 2, 2, 2]
        self.assertTrue(ind.cross_above(fast, slow))
        self.assertFalse(ind.cross_below(fast, slow))

    def test_kdj_and_bollinger(self):
        highs = [10, 11, 12, 13, 14, 15, 14, 13, 12, 11]
        lows = [9, 9, 9, 10, 10, 11, 10, 9, 8, 8]
        closes = [9.5, 10, 11, 12, 13, 14, 13, 12, 11, 10]
        k, d, j = ind.kdj(highs, lows, closes, 9)
        self.assertIsNotNone(k)
        upper, mid, lower = ind.bollinger(closes, 5)
        self.assertIsNotNone(upper)
        self.assertGreaterEqual(upper, mid)
        self.assertGreaterEqual(mid, lower)

    def test_values_from_dataframe_like(self):
        class Df:
            _data = [{'close': 1}, {'close': 2}]

            def to_dict(self, orient):
                return self._data
        self.assertEqual(values_from(Df(), 'close'), [1, 2])


class FactorTest(SimpleTestCase):
    def test_ma_uptrend_signal(self):
        result = calculate({'node_type': 'signal', 'indicator': 'ma', 'period': 5},
                           kline_context())
        self.assertEqual(result['direction'], 1)
        self.assertIn('ma', result['payload'])

    def test_compare_legacy_interface(self):
        result = calculate({'calculation': 'compare', 'threshold': 40}, kline_context(base=50))
        self.assertEqual(result['direction'], 1)

    def test_mean_legacy_interface(self):
        result = calculate({'calculation': 'mean', 'period': 5}, kline_context(base=50))
        self.assertEqual(result['direction'], 1)
        self.assertIn('value', result['payload'])

    def test_rsi_overbought_gives_negative_direction(self):
        ctx = kline_context()  # 严格上涨 -> RSI=100 超买
        result = calculate({
            'node_type': 'signal', 'indicator': 'rsi', 'period': 14,
            'threshold_overbought': 70, 'threshold_oversold': 30,
        }, ctx)
        self.assertEqual(result['direction'], -1)
        self.assertGreater(result['payload']['rsi'], 70)

    def test_filter_reports_payload(self):
        result = calculate({
            'node_type': 'filter', 'indicator': 'pct_change',
            'filter': {'op': 'keep', 'field': 'change_pct', 'threshold': 0},
        }, kline_context())
        self.assertIn('filtered', result['payload'])

    def test_verdict_weighted_sum(self):
        result = calculate({
            'verdict': {
                'method': 'weighted_sum',
                'components': [
                    {'indicator': 'ma', 'period': 5},
                    {'indicator': 'roc', 'period': 10},
                ],
            },
        }, kline_context())
        self.assertEqual(result['direction'], 1)

    def test_fallback_price_avoids_crash(self):
        result = calculate({'indicator': 'ma', 'period': 5}, fallback_context())
        self.assertIn('direction', result)

    def test_unknown_calculation_raises(self):
        with self.assertRaises(ValueError):
            calculate({'calculation': 'bogus'}, kline_context())


class DataContextBuilderTest(TestCase):
    def setUp(self):
        self.symbol = Symbol.objects.create(
            code='000001', name='测试标的', market='A',
        )

    def test_build_context_without_db_records(self):
        builder = DataContextBuilder()
        ctx = builder.build(self.symbol)
        self.assertEqual(ctx['symbol'], '000001')
        self.assertEqual(ctx['data_source'], 'database')
        self.assertIsNotNone(ctx['asof'])

    def test_snapshot_included_when_present(self):
        RealtimeSnapshot.objects.create(
            symbol=self.symbol, price='12.3400', change='1.20',
            volume=1000, turnover='12345.00', high='12.5000', low='12.1000',
            open_price='12.2000', pre_close='12.1800',
        )
        ctx = DataContextBuilder().build(self.symbol)
        self.assertEqual(ctx['snapshot']['price'], 12.34)

    def test_broker_fallback_when_no_db(self):
        class FakeDf:
            _data = [{'date': '2026-01-01', 'close': 10.0}]

            def to_dict(self, orient):
                return self._data

        class FakeBroker:
            def history_n(self, **kwargs):
                return FakeDf()

        ctx = DataContextBuilder(broker=FakeBroker(), prefer_db=False).build(self.symbol)
        self.assertEqual(ctx['data_source'], 'sdk')
        self.assertEqual(ctx['last_close'], 10.0)

    def test_context_merges_trigger_payload_without_overwrite(self):
        builder = DataContextBuilder()
        ctx = builder.build(self.symbol, context={'custom_key': 'x'})
        self.assertEqual(ctx['custom_key'], 'x')
        self.assertEqual(ctx['trigger_payload']['custom_key'], 'x')