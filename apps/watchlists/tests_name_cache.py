"""标的名称缓存（``SymbolNameCache``）与缓存优先的名称解析专项测试。

重点覆盖本轮优化的四个关键行为：

1. **缓存优先**：命中 ``Symbol`` 表/ 缓存时不产生任何网络请求；
2. **整市场批量**：批量解析多个代码，每个市场**只**拉一次在线表（旧实现是每代码一次）；
3. **离线降级**：在线源异常时回退过期缓存（stale-while-error），不清空已有数据；
4. **000xxx 二义不回归**：裸 ``000001`` 是平安银行，只有显式 ``sh000001`` 才是上证指数。
"""
# pylint: disable=import-outside-toplevel,protected-access  # 延迟导入；测试需访问私有缓存 TTL 函数
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pandas as pd
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.watchlists.models import Symbol, SymbolNameCache
from apps.watchlists.services import (
    fetch_market_name_map,
    normalize_index_lookup_code,
    normalize_market_hint,
    normalize_name_lookup_code,
    refresh_symbol_name_cache,
    resolve_symbol_name,
)

A_FRAME = pd.DataFrame({'code': ['600000.SH', '000001.SZ'], 'name': ['浦发银行', '平安银行']})
HK_FRAME = pd.DataFrame({'code': ['00700'], 'name': ['腾讯控股']})


class NameCodeNormalizationTest(TestCase):
    """代码与市场提示的归一（缓存能命中的前提）。"""

    def test_code_variants_collapse_to_same_key(self):
        for variant in ('SHSE.600000', '600000.SH', 'sh600000', '600000', ' 600000 '):
            self.assertEqual(normalize_name_lookup_code(variant), '600000', variant)

    def test_hk_code_zero_padded(self):
        """港股代码补足 5 位（AkShare 给 00700，用户可能写 700）。"""
        for variant in ('00700', '700', '0700.HK', '00700.HK'):
            self.assertEqual(normalize_name_lookup_code(variant), '00700', variant)

    def test_us_code_strips_dot_suffix(self):
        self.assertEqual(normalize_name_lookup_code('aapl.o'), 'AAPL')
        self.assertEqual(normalize_name_lookup_code('AAPL'), 'AAPL')

    def test_empty_code(self):
        self.assertEqual(normalize_name_lookup_code(''), '')
        self.assertEqual(normalize_name_lookup_code(None), '')

    def test_index_lookup_keeps_exchange_prefix(self):
        """指数查表必须保留 sh/sz 前缀（这是区分指数/个股的语义）。"""
        self.assertEqual(normalize_index_lookup_code('sh000001'), 'sh000001')
        self.assertEqual(normalize_index_lookup_code('SHSE.000001'), 'sh000001')
        self.assertEqual(normalize_index_lookup_code('sz399001'), 'sz399001')
        # 裸代码保持裸的，不猜交易所
        self.assertEqual(normalize_index_lookup_code('000001'), '000001')

    def test_market_hint_tolerates_exchange_aliases(self):
        """此前传 sh/SSE 等写法会让三个分支全部落空并静默退化。"""
        for alias in ('sh', 'SH', 'SSE', 'SHSE', 'szse', 'SZSE', 'A'):
            self.assertEqual(normalize_market_hint(alias), 'A', alias)
        self.assertEqual(normalize_market_hint('HK'), 'HK')
        for alias in ('us', 'USA', 'NASDAQ'):
            self.assertEqual(normalize_market_hint(alias), 'US', alias)
        self.assertEqual(normalize_market_hint(''), '')


class CacheFirstResolveTest(TestCase):
    """缓存优先：命中本地/缓存时零网络请求。"""

    databases = ['default', 'kline']

    def test_local_symbol_hit_makes_no_network_call(self):
        Symbol.objects.create(code='600000', name='浦发银行', market='A', exchange='SSE')
        with patch('apps.watchlists.services.fetch_market_name_map') as fetcher:
            self.assertEqual(resolve_symbol_name('600000', 'A'), '浦发银行')
        fetcher.assert_not_called()

    def test_cache_hit_makes_no_network_call(self):
        SymbolNameCache.objects.create(
            market='A', code='600519', name='贵州茅台', source='test', fetched_at=timezone.now())
        with patch('apps.watchlists.services.fetch_market_name_map') as fetcher:
            self.assertEqual(resolve_symbol_name('600519', 'A'), '贵州茅台')
        fetcher.assert_not_called()

    def test_cache_refetch_respects_ttl(self):
        """TTL 未到不重拉。"""
        SymbolNameCache.objects.create(
            market='A', code='600519', name='贵州茅台', source='test', fetched_at=timezone.now())
        with patch('apps.watchlists.services.fetch_market_name_map') as fetcher:
            resolve_symbol_name('600519', 'A')
        fetcher.assert_not_called()

    def test_expired_cache_triggers_single_market_refresh(self):
        SymbolNameCache.objects.create(
            market='A', code='600519', name='旧名称', source='test',
            fetched_at=timezone.now() - timedelta(days=400))
        with patch('apps.watchlists.services.fetch_market_name_map',
                   return_value=({'600519': '贵州茅台'}, 'fake')) as fetcher:
            self.assertEqual(resolve_symbol_name('600519', 'A'), '贵州茅台')
        fetcher.assert_called_once_with('A')
        # 刷新后的名称已落库
        self.assertEqual(SymbolNameCache.objects.get(market='A', code='600519').name, '贵州茅台')


class BulkFetchOnceTest(TestCase):
    """整市场批量：N 个代码只拉 1 次在线表（旧实现是 N 次）。"""

    databases = ['default', 'kline']

    def test_many_codes_trigger_single_fetch(self):
        calls = []

        def fake_fetch(market):
            calls.append(market)
            # 归一后的字典，与 fetch_market_name_map 的真实行为一致
            return ({'600000': '浦发银行', '000001': '平安银行'}, 'fake')

        with patch('apps.watchlists.services.fetch_market_name_map', side_effect=fake_fetch):
            names = [resolve_symbol_name(code, 'A') for code in
                     ('600000', '000001', '600000.SH', 'SHSE.600000', '000001.SZ')]
        self.assertEqual(calls, ['A'], '整批解析只应拉一次全市场')
        self.assertEqual(names, ['浦发银行', '平安银行', '浦发银行', '浦发银行', '平安银行'])

    def test_cache_persists_across_calls(self):
        with patch('apps.watchlists.services.fetch_market_name_map',
                   return_value=({'600000': '浦发银行'}, 'ak')):
            resolve_symbol_name('600000.SH', 'A')
        cached = SymbolNameCache.objects.get(market='A', code='600000')
        self.assertEqual(cached.name, '浦发银行')
        self.assertEqual(cached.source, 'ak')


class OfflineDegradationTest(TestCase):
    """在线源不可用时降级，不清空已有缓存。"""

    databases = ['default', 'kline']

    def test_falls_back_to_stale_cache_when_offline(self):
        SymbolNameCache.objects.create(
            market='A', code='600000', name='浦发银行', source='old',
            fetched_at=timezone.now() - timedelta(days=400))
        with patch('apps.watchlists.services.ak.stock_info_a_code_name',
                   side_effect=RuntimeError('网络不可用')):
            self.assertEqual(resolve_symbol_name('600000', 'A'), '浦发银行')
        # 关键：过期数据被保留，没有被清空
        self.assertTrue(SymbolNameCache.objects.filter(market='A', code='600000').exists())

    def test_unknown_code_returns_code_itself(self):
        with patch('apps.watchlists.services.fetch_market_name_map', return_value=({}, '')):
            self.assertEqual(resolve_symbol_name('999999', 'A'), '999999')

    def test_empty_code_returns_empty_string(self):
        self.assertEqual(resolve_symbol_name(''), '')
        self.assertEqual(resolve_symbol_name(None), '')

    def test_missing_name_column_does_not_crash(self):
        """在线源返回结构异常时应安全降级，而不是抛异常。"""
        empty_frame = pd.DataFrame({'code': ['600000'], 'title': ['浦发银行']})
        with patch('apps.watchlists.services.ak.stock_info_a_code_name', return_value=empty_frame):
            self.assertEqual(resolve_symbol_name('600000', 'A', refresh=True), '600000')


class IndexAmbiguityTest(TestCase):
    """000xxx 二义段：裸 000001 是平安银行，显式 sh000001 才是上证指数。"""

    databases = ['default', 'kline']

    def test_bare_000001_is_stock_not_index(self):
        """回归防护：曾因给裸代码猜 sh 前缀而把 000001 误判成上证指数。"""
        with patch('apps.watchlists.services.fetch_market_name_map',
                   return_value=({'000001': '平安银行'}, 'fake')):
            self.assertEqual(resolve_symbol_name('000001', 'A'), '平安银行')

    def test_explicit_sh000001_is_index(self):
        self.assertEqual(resolve_symbol_name('sh000001', 'A'), '上证指数')
        self.assertEqual(resolve_symbol_name('SHSE.000001', 'A'), '上证指数')

    def test_other_index_names_hit_offline_table(self):
        self.assertEqual(resolve_symbol_name('sh000300', 'A'), '沪深300')
        self.assertEqual(resolve_symbol_name('sz399006', 'A'), '创业板指')


class RefreshCacheTest(TestCase):
    """批量刷新：upsert 不产生重复行。"""

    databases = ['default', 'kline']

    def test_bulk_upsert_is_idempotent(self):
        first = refresh_symbol_name_cache('A', {'600000': '浦发银行'}, 'ak')
        second = refresh_symbol_name_cache('A', {'600000': '浦发银行新名'}, 'ak')
        self.assertEqual((first, second), (1, 1))
        self.assertEqual(SymbolNameCache.objects.filter(market='A').count(), 1)
        self.assertEqual(
            SymbolNameCache.objects.get(market='A', code='600000').name, '浦发银行新名')

    def test_fetch_market_name_map_normalizes(self):
        with patch('apps.watchlists.services.ak.stock_info_a_code_name', return_value=A_FRAME):
            name_map, source = fetch_market_name_map('A')
        self.assertEqual(name_map, {'600000': '浦发银行', '000001': '平安银行'})
        self.assertEqual(source, 'akshare_code_name')

    def test_unsupported_market_returns_empty(self):
        name_map, source = fetch_market_name_map('XX')
        self.assertEqual((name_map, source), ({}, ''))


class RefreshCommandTest(TestCase):
    """``manage.py refresh_symbol_names``。"""

    databases = ['default', 'kline']

    def test_stats_reports_empty(self):
        out = StringIO()
        call_command('refresh_symbol_names', '--stats', stdout=out)
        self.assertIn('名称缓存为空', out.getvalue())

    def test_stats_reports_rows(self):
        SymbolNameCache.objects.create(
            market='A', code='600000', name='浦发银行', source='ak', fetched_at=timezone.now())
        out = StringIO()
        call_command('refresh_symbol_names', '--stats', stdout=out)
        self.assertIn('1 条', out.getvalue())

    def test_refresh_writes_cache(self):
        with patch('apps.watchlists.services.fetch_market_name_map',
                   return_value=({'600000': '浦发银行', '000001': '平安银行'}, 'ak')):
            call_command('refresh_symbol_names', '--market', 'A')
        self.assertEqual(SymbolNameCache.objects.filter(market='A').count(), 2)

    def test_market_failure_keeps_cache_and_raises(self):
        """某市场失败不应清空其已有缓存，并以非零退出码结束。"""
        SymbolNameCache.objects.create(
            market='HK', code='00700', name='腾讯控股', source='old', fetched_at=timezone.now())
        with patch('apps.watchlists.services.fetch_market_name_map',
                   side_effect=RuntimeError('在线源不可用')):
            with self.assertRaises(CommandError):
                call_command('refresh_symbol_names', '--market', 'HK')
        self.assertEqual(
            SymbolNameCache.objects.get(market='HK', code='00700').name, '腾讯控股')
