"""基本面缓存与历史时点测试。"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.datasources.models import FundamentalSnapshot, FundamentalCacheMeta
from apps.watchlists.models import Symbol
from runner.fundamentals import FundamentalsProvider
from runner.fundamentals_cache import CachedFundamentalsProvider


class _FakeProvider(FundamentalsProvider):
    """可注入的测试 Provider：fetch 带副作用计数。"""
    name = 'fake'
    calls = 0

    def fetch_basic_info(self, symbol):
        _FakeProvider.calls += 1
        return {'market_cap': 123.0, 'industry': '银行'}

    def fetch_financial_indicators(self, symbol):
        return {'roe': 12.5}


class CachedFundamentalsProviderTest(TestCase):
    def setUp(self):
        self.symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
        real = _FakeProvider()
        self.cache = CachedFundamentalsProvider(real, ttl=timedelta(hours=24))
        _FakeProvider.calls = 0

    def test_first_call_hits_source_and_backfills(self):
        metrics = self.cache.fetch(self.symbol)
        self.assertEqual(metrics['market_cap'], 123.0)
        self.assertEqual(_FakeProvider.calls, 1)
        # 已回填缓存
        self.assertEqual(FundamentalSnapshot.objects.count(), 1)
        self.assertEqual(FundamentalCacheMeta.objects.get(symbol=self.symbol).miss_count, 1)

    def test_fresh_cache_hits_without_source(self):
        self.cache.fetch(self.symbol)  # 回填
        calls_after_backfill = _FakeProvider.calls
        metrics = self.cache.fetch(self.symbol)  # 应命中缓存
        self.assertEqual(metrics['market_cap'], 123.0)
        self.assertEqual(_FakeProvider.calls, calls_after_backfill)  # 未再回源
        self.assertEqual(FundamentalCacheMeta.objects.get(symbol=self.symbol).hit_count, 1)

    def test_no_past_data_for_historical_asof(self):
        # 只有一条未来时点的快照，查询更早时点不应返回
        future = timezone.now() + timedelta(days=365)
        FundamentalSnapshot.objects.create(symbol=self.symbol, asof=future, payload={'market_cap': 999.0})
        # 空 provider 不能回源（fetch_basic_info 已消费）
        metrics = self.cache.fetch(self.symbol, asof=timezone.now())
        # 过去时点无快照 → 回源；这里 fake 返回新数据
        self.assertEqual(_FakeProvider.calls, 1)

    def test_historical_asof_reads_latest_available(self):
        now = timezone.now()
        old = FundamentalSnapshot.objects.create(
            symbol=self.symbol, asof=now - timedelta(days=10), payload={'market_cap': 100.0})
        newer = FundamentalSnapshot.objects.create(
            symbol=self.symbol, asof=now - timedelta(days=5), payload={'market_cap': 150.0})
        metrics = self.cache.fetch(self.symbol, asof=now - timedelta(days=3))
        self.assertEqual(metrics['market_cap'], 150.0)

    def test_expired_cache_refreshes_source(self):
        old = FundamentalSnapshot.objects.create(
            symbol=self.symbol,
            asof=timezone.now() - timedelta(days=2),  # 超过 24h TTL
            payload={'market_cap': 100.0})
        _FakeProvider.calls = 0
        metrics = self.cache.fetch(self.symbol)
        self.assertEqual(metrics['market_cap'], 123.0)  # 回源刷新
        self.assertEqual(_FakeProvider.calls, 1)

    def test_context_with_asof(self):
        FundamentalSnapshot.objects.create(
            symbol=self.symbol, asof=timezone.now() - timedelta(days=1), payload={'roe': 9.0})
        _FakeProvider.calls = 0
        ctx = self.cache.context(self.symbol, asof=timezone.now() - timedelta(hours=12))
        self.assertEqual(ctx['metrics']['roe'], 9.0)
        self.assertEqual(_FakeProvider.calls, 0)  # 命中缓存，未回源

    def test_provider_type_validation(self):
        with self.assertRaises(TypeError):
            CachedFundamentalsProvider(None)