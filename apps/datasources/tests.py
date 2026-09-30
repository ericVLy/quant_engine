# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
import logging
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APITestCase, APIClient, APITransactionTestCase

from apps.watchlists.models import Symbol
from apps.datasources.models import (
    RealtimeSnapshot, KLineSyncLog,
    ensure_kline_table, get_runtime_kline_model
)
from apps.datasources.services import (
    sync_kline_for_symbol, sync_all_symbols,
    get_kline_table_name, query_kline_table, fetch_kline_from_ashare
)
from apps.datasources.ashare import get_price_day_tx, get_price_sina

logger = logging.getLogger(__name__)


class RealtimeSnapshotAPITest(APITestCase):
    """测试实时快照只读接口"""

    def setUp(self):
        logger.info("=== RealtimeSnapshotAPITest 开始 ===")
        self.client = APIClient()
        self.symbol = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        self.snapshot = RealtimeSnapshot.objects.create(
            symbol=self.symbol,
            price=Decimal('12.34'),
            change=Decimal('1.23'),
            volume=1000000,
            turnover=Decimal('12345678.90'),
            high=Decimal('12.50'),
            low=Decimal('12.20'),
            open_price=Decimal('12.30'),
            pre_close=Decimal('12.20')
        )
        self.list_url = '/api/datasources/snapshots/'
        logger.info(f"创建标的: {self.symbol.code}, 快照价格: {self.snapshot.price}")

    def test_list_snapshots(self):
        logger.info("测试列出所有快照")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['symbol']['code'], '000001')
        logger.info("快照列表返回记录数: 1")

    def test_retrieve_snapshot_by_symbol_id(self):
        logger.info("测试按 symbol ID 获取快照")
        url = f'/api/datasources/snapshots/{self.symbol.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['symbol']['code'], '000001')
        self.assertEqual(response.data['price'], '12.3400')
        logger.info("成功获取快照详情")


class KLineSyncLogAPITest(APITestCase):
    """测试同步日志只读接口"""

    def setUp(self):
        logger.info("=== KLineSyncLogAPITest 开始 ===")
        self.client = APIClient()
        self.symbol = Symbol.objects.create(
            code='000002', name='万科A', market='A', exchange='SZSE'
        )
        self.log = KLineSyncLog.objects.create(
            symbol=self.symbol,
            sync_type='daily',
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 10),
            records_added=5,
            records_skipped=2,
            status='success',
            error_msg=''
        )
        self.list_url = '/api/datasources/sync-logs/'
        logger.info(f"创建同步日志: {self.symbol.code}, 添加 {self.log.records_added} 条")

    def test_list_logs(self):
        logger.info("测试列出所有同步日志")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(len(response.data['results']), 1)
        logger.info("日志列表返回记录数: 1")

    def test_retrieve_log(self):
        logger.info("测试获取单个同步日志")
        url = f'/api/datasources/sync-logs/{self.log.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['symbol']['code'], '000002')
        logger.info("成功获取同步日志详情")


class AshareKLineFetchTest(TestCase):
    """测试 ashare 模块返回的数据规范化"""

    @patch('apps.datasources.services.ashare_get_price')
    def test_fetch_kline_from_ashare_normalizes_schema(self, mock_get_price):
        index = pd.to_datetime(['2024-01-02', '2024-01-03'])
        df = pd.DataFrame(
            {
                'open': [10.0, 11.0],
                'high': [11.0, 12.0],
                'low': [9.5, 10.5],
                'close': [10.8, 11.7],
                'volume': [1000, 1200],
            },
            index=index,
        )
        mock_get_price.return_value = df

        symbol = Symbol.objects.create(code='000001', name='平安银行', market='A', exchange='SZSE')
        result = fetch_kline_from_ashare(symbol, date(2024, 1, 2), date(2024, 1, 3), adjust='qfq')

        self.assertIsNotNone(result)
        self.assertFalse(result.empty)
        self.assertIn('date', result.columns)
        self.assertIn('amount', result.columns)
        self.assertIn('adj_factor', result.columns)
        self.assertIn('turnover_rate', result.columns)
        self.assertEqual(result.iloc[0]['date'], date(2024, 1, 2))
        self.assertAlmostEqual(result.iloc[0]['amount'], 10800.0)
        self.assertEqual(str(result.iloc[0]['adj_factor']), '1.0')

    @patch('apps.datasources.ashare.requests.get')
    def test_get_price_day_tx_handles_empty_param_error(self, mock_get):
        mock_get.return_value.json.return_value = {'code': 0, 'msg': 'param error', 'data': []}
        result = get_price_day_tx('000426', end_date=date(2026, 9, 1), count=10, frequency='1d')
        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    @patch('apps.datasources.ashare.requests.get')
    def test_get_price_sina_accepts_date_end_date(self, mock_get):
        mock_get.return_value.json.return_value = [
            {'day': '2024-01-02', 'open': '10.0', 'high': '11.0', 'low': '9.5', 'close': '10.8', 'volume': '1000'}
        ]
        result = get_price_sina('sz000001', end_date=date(2024, 1, 2), count=10, frequency='1d')
        self.assertFalse(result.empty)
        self.assertEqual(str(result.index[0]), '2024-01-02 00:00:00')


class KLineAPITest(APITransactionTestCase):
    """测试 K 线查询和同步接口"""
    databases = ['default', 'kline']

    def setUp(self):
        logger.info("=== KLineAPITest 开始 ===")
        self.client = APIClient()
        self.symbol = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        ensure_kline_table(self.symbol)
        runtime_model = get_runtime_kline_model(self.symbol)
        runtime_model.objects.using('kline').all().delete()
        for i in range(1, 6):
            runtime_model.objects.using('kline').create(
                symbol_id=self.symbol.id,
                date=date(2024, 1, 10 + i),
                open=Decimal('10.0') + Decimal(i),
                high=Decimal('10.5') + Decimal(i),
                low=Decimal('9.5') + Decimal(i),
                close=Decimal('10.2') + Decimal(i),
                volume=1000000 * i,
                amount=Decimal('1000000') * i,
                adj_factor=Decimal('1.0'),
                turnover_rate=Decimal('0.5') * Decimal(i),
            )
        logger.info("预置 5 条 K 线数据，日期 2024-01-11 至 2024-01-15")
        self.query_url = '/api/datasources/kline/query/'
        self.sync_url = '/api/datasources/kline/sync/'

    def test_query_kline_success(self):
        logger.info("测试成功查询 K 线")
        response = self.client.get(self.query_url, {
            'symbol': '000001',
            'start': '2024-01-11',
            'end': '2024-01-15'
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 5)
        first = response.data['results'][0]
        self.assertIn('symbol', first)
        self.assertIn('date', first)
        self.assertIn('extra', first)
        # 按 Decimal 数值断言而非字符串：SQLite 下 adj_factor 返回 '1'，
        # MySQL DECIMAL(6,6) 才会补齐 '1.000000'。
        self.assertEqual(Decimal(str(first['extra']['adj_factor'])), Decimal('1.0'))
        logger.info(f"查询成功，返回 {response.data['count']} 条记录")

    def test_query_kline_missing_params(self):
        logger.info("测试缺少日期参数")
        response = self.client.get(self.query_url, {'symbol': '000001'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('start 和 end 日期必填', response.data['detail'])
        logger.info("返回预期的错误信息")

    def test_query_kline_symbol_not_found(self):
        logger.info("测试查询不存在的标的")
        response = self.client.get(self.query_url, {
            'symbol': '999999',
            'start': '2024-01-01',
            'end': '2024-01-05'
        })
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        logger.info("返回 404 错误")

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_single(self, mock_hist):
        logger.info("测试同步单个标的 K 线（mock 数据）")
        df = pd.DataFrame({
            '日期': ['2024-01-01', '2024-01-02'],
            '开盘': [10.0, 10.5],
            '收盘': [10.2, 10.7],
            '最高': [10.5, 11.0],
            '最低': [9.8, 10.2],
            '成交量': [1000000, 1200000],
            '成交额': [10200000, 12840000],
            '涨跌幅': [0.02, 0.05],
            '涨跌额': [0.2, 0.5],
            '换手率': [0.5, 0.6]
        })
        mock_hist.return_value = df
        logger.info("已准备 mock DataFrame，包含 2 条数据")

        response = self.client.post(self.sync_url, {
            'symbol': '000001',
            'start_date': '2024-01-01',
            'end_date': '2024-01-02',
            'adjust': 'qfq'
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['added'], 2)
        self.assertEqual(response.data['skipped'], 0)
        self.assertIsNone(response.data['error'])
        self.assertEqual(len(query_kline_table(self.symbol, date(2024, 1, 1), date(2024, 1, 15))), 7)
        log = KLineSyncLog.objects.latest('created_at')
        self.assertEqual(log.records_added, 2)
        self.assertEqual(log.status, 'success')
        logger.info(f"同步完成，新增 {response.data['added']} 条，跳过 {response.data['skipped']} 条")

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_all(self, mock_hist):
        logger.info("测试同步所有标的 K 线（mock 数据）")
        df = pd.DataFrame({
            '日期': ['2024-01-01'],
            '开盘': [10.0],
            '收盘': [10.2],
            '最高': [10.5],
            '最低': [9.8],
            '成交量': [1000000],
            '成交额': [10200000],
            '涨跌幅': [0.02],
            '涨跌额': [0.2],
            '换手率': [0.5]
        })
        mock_hist.return_value = df
        logger.info("已准备 mock DataFrame，包含 1 条数据")

        response = self.client.post(self.sync_url, {
            'symbol': 'all',
            'start_date': '2024-01-01',
            'end_date': '2024-01-01'
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)
        result = response.data['results'][0]
        self.assertEqual(result['symbol'], '000001')
        self.assertEqual(result['added'], 1)
        logger.info(f"同步所有完成，标的 {result['symbol']} 新增 {result['added']} 条")


class ServicesTest(TransactionTestCase):
    """测试数据服务函数"""
    databases = ['default', 'kline']

    def setUp(self):
        logger.info("=== ServicesTest 开始 ===")
        self.symbol_a = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        runtime_model = get_runtime_kline_model(self.symbol_a)
        runtime_model.objects.using('kline').all().delete()
        logger.info(f"创建测试标的: {self.symbol_a.code}")

    def test_dynamic_kline_table_name_and_query(self):
        logger.info("测试按股票编码创建动态分表和查询")
        table_name = get_kline_table_name(self.symbol_a)
        self.assertEqual(table_name, 'kline_a_000001')
        self.assertTrue(table_name.startswith('kline_'))

        runtime_model = get_runtime_kline_model(self.symbol_a)
        runtime_model.objects.using('kline').create(
            symbol_id=self.symbol_a.id,
            date=date(2024, 1, 5),
            open=Decimal('10.1'),
            high=Decimal('10.8'),
            low=Decimal('9.9'),
            close=Decimal('10.6'),
            volume=2000000,
            amount=Decimal('20000000'),
            adj_factor=Decimal('1.0'),
            turnover_rate=Decimal('0.5'),
        )


        rows = query_kline_table(self.symbol_a, date(2024, 1, 5), date(2024, 1, 5))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['symbol'], '000001')
        # 按 Decimal 数值断言而非字符串：SQLite 不保留 DECIMAL 精度展示
        # （'10.6'），MySQL DECIMAL(10,4) 才会补齐 '10.6000'。
        self.assertEqual(Decimal(str(rows[0]['close'])), Decimal('10.6'))
        logger.info(f"动态分表查询返回 {len(rows)} 条，表名为 {table_name}")

    def test_runtime_table_query_uses_table_and_date_only(self):
        runtime_model = get_runtime_kline_model(self.symbol_a)
        runtime_model.objects.using('kline').create(
            symbol_id=self.symbol_a.id + 999,
            date=date(2024, 1, 6),
            open=Decimal('10.1'),
            high=Decimal('10.8'),
            low=Decimal('9.9'),
            close=Decimal('10.6'),
            volume=2000000,
            amount=Decimal('20000000'),
            adj_factor=Decimal('1.0'),
            turnover_rate=Decimal('0.5'),
        )

        rows = query_kline_table(self.symbol_a, date(2024, 1, 6), date(2024, 1, 6))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['date'], date(2024, 1, 6))

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_for_symbol_new_data(self, mock_hist):
        logger.info("测试同步新数据（无冲突）")
        df = pd.DataFrame({
            '日期': ['2024-01-01'],
            '开盘': [10.0],
            '收盘': [10.2],
            '最高': [10.5],
            '最低': [9.8],
            '成交量': [1000000],
            '成交额': [10200000],
            '涨跌幅': [0.02],
            '涨跌额': [0.2],
            '换手率': [0.5]
        })
        mock_hist.return_value = df
        added, skipped, error = sync_kline_for_symbol(
            self.symbol_a,
            start_date='2024-01-01',
            end_date='2024-01-01'
        )
        self.assertEqual(added, 1)
        self.assertEqual(skipped, 0)
        self.assertIsNone(error)
        self.assertEqual(len(query_kline_table(self.symbol_a, date(2024, 1, 1), date(2024, 1, 1))), 1)
        logger.info(f"新增 {added} 条，跳过 {skipped} 条")

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_for_symbol_skip_existing(self, mock_hist):
        logger.info("测试同步已存在的数据（应跳过）")
        runtime_model = get_runtime_kline_model(self.symbol_a)
        runtime_model.objects.using('kline').create(
            symbol_id=self.symbol_a.id,
            date=date(2024, 1, 1),
            open=Decimal('10.0'),
            high=Decimal('10.5'),
            low=Decimal('9.8'),
            close=Decimal('10.2'),
            volume=1000000,
            amount=Decimal('10200000'),
            adj_factor=Decimal('1.0'),
            turnover_rate=Decimal('0.5'),
        )
        df = pd.DataFrame({
            '日期': ['2024-01-01'],
            '开盘': [10.0],
            '收盘': [10.2],
            '最高': [10.5],
            '最低': [9.8],
            '成交量': [1000000],
            '成交额': [10200000],
            '涨跌幅': [0.02],
            '涨跌额': [0.2],
            '换手率': [0.5]
        })
        mock_hist.return_value = df
        added, skipped, error = sync_kline_for_symbol(
            self.symbol_a,
            start_date='2024-01-01',
            end_date='2024-01-01'
        )
        self.assertEqual(added, 0)
        self.assertEqual(skipped, 1)
        self.assertIsNone(error)
        # 增量优化：区间首尾均已有数据 → 不应发起任何远端拉取
        mock_hist.assert_not_called()
        logger.info(f"新增 {added} 条，跳过 {skipped} 条（未触发远端拉取）")

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_incremental_narrows_window(self, mock_hist):
        """库内已有 2024-01-01~2024-01-05 中的 01-01 与 01-05，增量同步应只拉缺口之后的窗口。"""
        runtime_model = get_runtime_kline_model(self.symbol_a)
        for day, close in (('2024-01-01', '10.0'), ('2024-01-05', '10.4')):
            runtime_model.objects.using('kline').create(
                symbol_id=self.symbol_a.id,
                date=date.fromisoformat(day),
                open=Decimal('10.0'),
                high=Decimal('10.5'),
                low=Decimal('9.8'),
                close=Decimal(close),
                volume=1000000,
                amount=Decimal('10200000'),
                adj_factor=Decimal('1.0'),
                turnover_rate=Decimal('0.5'),
            )
        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-08'],
            '开盘': [10.0],
            '收盘': [10.3],
            '最高': [10.5],
            '最低': [9.8],
            '成交量': [1000000],
            '成交额': [10200000],
            '涨跌幅': [0.02],
            '涨跌额': [0.2],
            '换手率': [0.5],
        })

        added, _, error = sync_kline_for_symbol(
            self.symbol_a,
            start_date='2024-01-01',
            end_date='2024-01-10',
        )
        self.assertIsNone(error)
        self.assertEqual(added, 1)
        # 头部已覆盖（min <= start）：拉取窗口收窄为 (max_existing, end] = 2024-01-06 起
        mock_hist.assert_called_once()
        kwargs = mock_hist.call_args.kwargs
        self.assertEqual(kwargs['start_date'], date(2024, 1, 6))
        self.assertEqual(kwargs['end_date'], date(2024, 1, 10))

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_head_gap_fetches_full_window(self, mock_hist):
        """库内最早一条晚于 start_date（头部可能缺口）→ 保持全量拉取，由逐行去重兜底。"""
        runtime_model = get_runtime_kline_model(self.symbol_a)
        runtime_model.objects.using('kline').create(
            symbol_id=self.symbol_a.id,
            date=date(2024, 1, 5),
            open=Decimal('10.0'),
            high=Decimal('10.5'),
            low=Decimal('9.8'),
            close=Decimal('10.4'),
            volume=1000000,
            amount=Decimal('10200000'),
            adj_factor=Decimal('1.0'),
            turnover_rate=Decimal('0.5'),
        )
        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-01'],
            '开盘': [10.0],
            '收盘': [10.2],
            '最高': [10.5],
            '最低': [9.8],
            '成交量': [1000000],
            '成交额': [10200000],
            '涨跌幅': [0.02],
            '涨跌额': [0.2],
            '换手率': [0.5],
        })

        _, _, error = sync_kline_for_symbol(
            self.symbol_a,
            start_date='2024-01-01',
            end_date='2024-01-10',
        )
        self.assertIsNone(error)
        mock_hist.assert_called_once()
        kwargs = mock_hist.call_args.kwargs
        self.assertEqual(kwargs['start_date'], date(2024, 1, 1))
        self.assertEqual(kwargs['end_date'], date(2024, 1, 10))

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_all_symbols(self, mock_hist):
        logger.info("测试同步所有标的（仅 A 股）")
        df = pd.DataFrame({
            '日期': ['2024-01-01'],
            '开盘': [10.0],
            '收盘': [10.2],
            '最高': [10.5],
            '最低': [9.8],
            '成交量': [1000000],
            '成交额': [10200000],
            '涨跌幅': [0.02],
            '涨跌额': [0.2],
            '换手率': [0.5]
        })
        mock_hist.return_value = df
        results = sync_all_symbols(start_date='2024-01-01', end_date='2024-01-01')
        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res['symbol'], '000001')
        self.assertEqual(res['added'], 1)
        self.assertIsNone(res['error'])
        self.assertTrue(KLineSyncLog.objects.filter(symbol=self.symbol_a).exists())
        logger.info(f"同步所有完成，标的 {res['symbol']} 新增 {res['added']} 条")

class AshareCodeNormalizeTest(TestCase):
    """A 股代码归一化：指数与个股区分（见 watchlists.services 统一规则）。"""

    def test_index_only_prefixes(self):
        from apps.datasources.ashare import _normalize_ashare_code
        # 深市指数专属段
        self.assertEqual(_normalize_ashare_code('399001'), 'sz399001')
        self.assertEqual(_normalize_ashare_code('399006'), 'sz399006')
        # 中证/申万指数段 → 沪市
        self.assertEqual(_normalize_ashare_code('930955'), 'sh930955')
        self.assertEqual(_normalize_ashare_code('880001'), 'sh880001')

    def test_explicit_prefix_preserves_index_semantics(self):
        from apps.datasources.ashare import _normalize_ashare_code
        # 显式 sh 前缀保留沪市指数语义（sh000300 沪深300）
        self.assertEqual(_normalize_ashare_code('sh000300'), 'sh000300')
        self.assertEqual(_normalize_ashare_code('SH000905'), 'sh000905')
        # 显式 sz 前缀保留深市个股语义
        self.assertEqual(_normalize_ashare_code('sz000001'), 'sz000001')

    def test_stock_codes_unchanged(self):
        from apps.datasources.ashare import _normalize_ashare_code
        self.assertEqual(_normalize_ashare_code('600000'), 'sh600000')
        self.assertEqual(_normalize_ashare_code('000426'), 'sz000426')
        self.assertEqual(_normalize_ashare_code('300750'), 'sz300750')
        self.assertEqual(_normalize_ashare_code('688981'), 'sh688981')

    def test_short_code_zero_pad(self):
        from apps.datasources.ashare import _normalize_ashare_code
        self.assertEqual(_normalize_ashare_code('426'), 'sz000426')


class MultiMarketKlineFetchTest(APITransactionTestCase):
    """港股/美股日线拉取（akshare 东财）与按市场分派防回归。

    背景：拉取层原先只实现 A 股（ashare/sina/腾讯），新增港股标的时
    `_normalize_ashare_code('00700')` 会把港股代码补零成 `000700` 当深市
    A 股拉取（错误行情静默入库）；美股代码 sina 不识别，永远返回空。
    修复后 HK/US 分派到 akshare 东财接口，并禁止再落入 A 股 ashare 通道。
    """

    databases = ['default', 'kline']

    def setUp(self):
        self.symbol_hk = Symbol.objects.create(
            code='00700', name='腾讯控股', market='HK', exchange='HKEX'
        )
        self.symbol_us = Symbol.objects.create(
            code='AAPL', name='苹果', market='US', exchange='NASDAQ'
        )
        self.symbol_a_index = Symbol.objects.create(
            code='000300', name='沪深300', market='A', exchange='SSE'
        )

    @staticmethod
    def _akshare_hist_df():
        """akshare 东财日线接口的中文列名返回结构。"""
        return pd.DataFrame({
            '日期': ['2024-01-02', '2024-01-03'],
            '开盘': [300.0, 302.0],
            '收盘': [301.0, 303.0],
            '最高': [305.0, 306.0],
            '最低': [299.0, 300.5],
            '成交量': [1000000, 1100000],
            '成交额': [301000000.0, 333300000.0],
            '涨跌幅': [0.5, 0.66],
        })

    # ---- 港股 ----

    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_fetch_hk_kline_via_akshare(self, mock_hist):
        mock_hist.return_value = self._akshare_hist_df()
        df = fetch_kline_from_ashare(self.symbol_hk, '2024-01-01', '2024-01-31', 'qfq')
        self.assertEqual(len(df), 2)
        kwargs = mock_hist.call_args.kwargs
        self.assertEqual(kwargs['symbol'], '00700')
        self.assertEqual(kwargs['start_date'], '20240101')
        self.assertEqual(kwargs['end_date'], '20240131')
        self.assertEqual(kwargs['adjust'], 'qfq')
        for col in ('date', 'open', 'high', 'low', 'close', 'volume', 'amount'):
            self.assertIn(col, df.columns)

    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_fetch_hk_short_code_padded_to_five(self, mock_hist):
        mock_hist.return_value = self._akshare_hist_df()
        symbol = Symbol.objects.create(code='700', name='腾讯', market='HK', exchange='HKEX')
        fetch_kline_from_ashare(symbol, '2024-01-01', '2024-01-31')
        self.assertEqual(mock_hist.call_args.kwargs['symbol'], '00700')

    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_fetch_hk_empty_result_raises_located_error(self, mock_hist):
        mock_hist.return_value = pd.DataFrame()
        with self.assertRaises(ValueError) as ctx:
            fetch_kline_from_ashare(self.symbol_hk, '2024-01-01', '2024-01-31')
        self.assertIn('00700', str(ctx.exception))

    # ---- 美股 ----

    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    def test_fetch_us_kline_prefix_fallback(self, mock_hist):
        # 105(NASDAQ) 失败 → 106(NYSE) 命中
        mock_hist.side_effect = [Exception('not found'), self._akshare_hist_df()]
        df = fetch_kline_from_ashare(self.symbol_us, '2024-01-01', '2024-01-31', 'qfq')
        self.assertEqual(len(df), 2)
        self.assertEqual(mock_hist.call_count, 2)
        self.assertEqual(mock_hist.call_args_list[0].kwargs['symbol'], '105.AAPL')
        self.assertEqual(mock_hist.call_args_list[1].kwargs['symbol'], '106.AAPL')

    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    def test_fetch_us_explicit_prefix_used_directly(self, mock_hist):
        mock_hist.return_value = self._akshare_hist_df()
        symbol = Symbol.objects.create(code='105.TSLA', name='特斯拉', market='US', exchange='NASDAQ')
        fetch_kline_from_ashare(symbol, '2024-01-01', '2024-01-31')
        self.assertEqual(mock_hist.call_count, 1)
        self.assertEqual(mock_hist.call_args.kwargs['symbol'], '105.TSLA')

    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    def test_fetch_us_all_prefixes_fail_raises_located_error(self, mock_hist):
        mock_hist.side_effect = Exception('connection down')
        with self.assertRaises(ValueError) as ctx:
            fetch_kline_from_ashare(self.symbol_us, '2024-01-01', '2024-01-31')
        message = str(ctx.exception)
        self.assertIn('AAPL', message)
        self.assertIn('NASDAQ/NYSE/AMEX', message)
        self.assertIn('connection down', message)

    # ---- 分派防回归：HK/US 不得再走 A 股 ashare 通道 ----

    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    @patch('apps.datasources.services.ashare_lib.get_price')
    def test_hk_us_never_routed_through_ashare(self, mock_get_price, mock_hk, mock_us):
        mock_hk.return_value = self._akshare_hist_df()
        mock_us.return_value = self._akshare_hist_df()
        fetch_kline_from_ashare(self.symbol_hk, '2024-01-01', '2024-01-31')
        fetch_kline_from_ashare(self.symbol_us, '2024-01-01', '2024-01-31')
        mock_get_price.assert_not_called()

    # ---- A 股 000xxx 二义段：exchange 标注沪市时显式 sh 前缀 ----

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_a_share_sse_ambiguous_code_gets_sh_prefix(self, mock_hist):
        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-02'], '开盘': [3400.0], '收盘': [3410.0],
            '最高': [3420.0], '最低': [3390.0], '成交量': [100000], '成交额': [3.4e10],
        })
        fetch_kline_from_ashare(self.symbol_a_index, '2024-01-01', '2024-01-31')
        # exchange=SSE 的 000300（沪深300）必须以 sh 前缀传给 ashare，保留沪市指数语义
        self.assertEqual(mock_hist.call_args.kwargs['symbol'], 'sh000300')

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_a_share_default_ambiguous_code_unchanged(self, mock_hist):
        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-02'], '开盘': [10.0], '收盘': [10.2],
            '最高': [10.5], '最低': [9.8], '成交量': [1000000], '成交额': [1.02e7],
        })
        symbol = Symbol.objects.create(code='000001', name='平安银行', market='A', exchange='SZSE')
        fetch_kline_from_ashare(symbol, '2024-01-01', '2024-01-31')
        # 缺省保守按深市个股，代码原样传递（ashare 层缺省 sz 前缀）
        self.assertEqual(mock_hist.call_args.kwargs['symbol'], '000001')

    # ---- 新增港股/美股标的 → 同步入库 端到端 ----

    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_sync_new_hk_symbol_end_to_end(self, mock_hist):
        mock_hist.return_value = self._akshare_hist_df()
        added, skipped, error = sync_kline_for_symbol(
            self.symbol_hk, 'daily',
            start_date='2024-01-01', end_date='2024-01-31', adjust='qfq',
        )
        self.assertIsNone(error)
        self.assertEqual(added, 2)
        self.assertEqual(skipped, 0)
        rows = query_kline_table(self.symbol_hk, date(2024, 1, 1), date(2024, 1, 31))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['extra']['currency'], 'HKD')
        self.assertEqual(rows[0]['symbol'], '00700')

    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    def test_sync_new_us_symbol_end_to_end(self, mock_hist):
        mock_hist.return_value = self._akshare_hist_df()
        added, _, error = sync_kline_for_symbol(
            self.symbol_us, 'daily',
            start_date='2024-01-01', end_date='2024-01-31', adjust='qfq',
        )
        self.assertIsNone(error)
        self.assertEqual(added, 2)
        rows = query_kline_table(self.symbol_us, date(2024, 1, 1), date(2024, 1, 31))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['extra']['split_factor'], 1.0)

    # ---- 同步失败语义：拉取异常透传为可定位 error（不静默） ----

    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_sync_hk_failure_returns_located_error(self, mock_hist):
        mock_hist.side_effect = Exception('network unreachable')
        added, _, error = sync_kline_for_symbol(
            self.symbol_hk, 'daily',
            start_date='2024-01-01', end_date='2024-01-31', adjust='qfq',
        )
        self.assertEqual(added, 0)
        self.assertIn('network unreachable', error or '')
