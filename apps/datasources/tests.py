# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
import logging
from datetime import date, timedelta
from unittest import mock

import pandas as pd
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
    get_kline_table_name, query_kline_table, fetch_kline_from_ashare,
    fetch_kline_from_ashare_tx, _tx_window_filter,
    _is_suspect_thin_kline,
)
from apps.datasources.ashare import (
    get_price_day_tx, get_price_sina,
    get_price_day_tx_hk, get_price_day_tx_us, get_price_day_sina_us,
    _normalize_hk_code_tx, _us_exchange_candidates, _fetch_sina_symbol,
)

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
    def test_sync_kline_rebinds_orphan_rows_after_symbol_recreated(self, mock_hist):
        """删除后重建同名标的（symbol_id 变化）→ 应认领旧行而非重复拉取。

        复现场景：主库 Symbol 被删除后重新添加同一 code，新记录拿到新的 symbol_id；
        而 K 线库按``market + code`` 命名分表并被保留，旧行的 symbol_id 仍是已删除的旧 id。
        """
        runtime_model = get_runtime_kline_model(self.symbol_a)
        orphan_symbol_id = self.symbol_a.id
        runtime_model.objects.using('kline').create(
            symbol_id=orphan_symbol_id,
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

        # 模拟「删除 → 重新添加」：新 Symbol 拿到不同 id，但分表名不变（kline_a_000001）
        self.symbol_a.delete()
        recreated = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        self.assertNotEqual(recreated.id, orphan_symbol_id)

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
            '换手率': [0.5],
        })
        mock_hist.return_value = df

        added, skipped, error = sync_kline_for_symbol(
            recreated, start_date='2024-01-01', end_date='2024-01-05',
        )

        self.assertIsNone(error)
        # 旧行已被认领 → 头部已覆盖 → 窗口收窄为 (max_existing, end] = 2024-01-02 起，
        # 关键是不再把已入库的 01-01 重拉一遍。
        kwargs = mock_hist.call_args.kwargs
        self.assertEqual(kwargs['start_date'], date(2024, 1, 2))
        self.assertEqual(added, 0)
        self.assertEqual(skipped, 1)
        # 旧行改挂到新 symbol_id，不产生重复行
        self.assertEqual(
            runtime_model.objects.using('kline').filter(symbol_id=recreated.id).count(), 1
        )
        self.assertEqual(
            runtime_model.objects.using('kline').filter(symbol_id=orphan_symbol_id).count(), 0
        )

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_rebuild_main_db_keeps_old_kline_db(self, mock_hist):
        """主库重建后标的拿到不同 id（旧行成孤儿）→ 应认领全部旧行并正确收窄增量窗口。

        SQLite 的 AUTOINCREMENT 不复用已删除的 id，因此「重建主库」与「删除后重加」
        都会让新 Symbol 拿到不同的 id：分表名由 ``market + code`` 决定而保持不变，
        表内旧行的 symbol_id 却指向一个已不存在的记录——这正是重复拉取的成因。
        """
        runtime_model = get_runtime_kline_model(self.symbol_a)
        orphan_symbol_id = self.symbol_a.id
        for day in ('2024-01-02', '2024-01-03'):
            runtime_model.objects.using('kline').create(
                symbol_id=orphan_symbol_id,
                date=date.fromisoformat(day),
                open=Decimal('10.0'),
                high=Decimal('10.5'),
                low=Decimal('9.8'),
                close=Decimal('10.2'),
                volume=1000000,
                amount=Decimal('10200000'),
                adj_factor=Decimal('1.0'),
                turnover_rate=Decimal('0.5'),
            )

        self.symbol_a.delete()
        recreated = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        self.assertNotEqual(recreated.id, orphan_symbol_id)

        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-08'],
            '开盘': [10.0], '收盘': [10.3], '最高': [10.5], '最低': [9.8],
            '成交量': [1000000], '成交额': [10200000],
            '涨跌幅': [0.02], '涨跌额': [0.2], '换手率': [0.5],
        })

        added, skipped, error = sync_kline_for_symbol(
            recreated, start_date='2024-01-01', end_date='2024-01-10',
        )

        self.assertIsNone(error)
        # 旧行已认领（头部覆盖到 01-03，但请求起点 01-01 早于它→ 按既有规则走全量窗口，
        # 逐行去重兜底）。关键断言：旧行没有被重复拉取/重复写入。
        self.assertEqual(skipped, 0)
        self.assertEqual(added, 1)
        # 认领后共 3 行（01-02、01-03 旧行 + 01-08 新行），同一交易日不出现两条
        self.assertEqual(len(query_kline_table(
            recreated, date(2024, 1, 1), date(2024, 1, 10)
        )), 3)
        self.assertEqual(
            runtime_model.objects.using('kline').filter(symbol_id=orphan_symbol_id).count(), 0
        )

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_repairs_existing_duplicate_rows(self, mock_hist):
        """修复前已累积重复行的库：认领时应删掉撞日期的旧行，同一交易日只留一条。

        这是「重复拉取」缺陷的历史遗留现场——旧行（symbol_id=已删id）与新行并存，
        仅靠 UNIQUE(symbol_id, date) 约束拦不住（symbol_id 不同即视为不同行）。
        """
        runtime_model = get_runtime_kline_model(self.symbol_a)
        orphan_symbol_id = self.symbol_a.id
        # 旧行（孤儿）
        runtime_model.objects.using('kline').create(
            symbol_id=orphan_symbol_id,
            date=date(2024, 1, 2),
            open=Decimal('10.0'), high=Decimal('10.5'), low=Decimal('9.8'),
            close=Decimal('10.2'), volume=1000000, amount=Decimal('10200000'),
            adj_factor=Decimal('1.0'), turnover_rate=Decimal('0.5'),
        )

        self.symbol_a.delete()
        recreated = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        self.assertNotEqual(recreated.id, orphan_symbol_id)

        # 修复前的那次重复拉取，已经为新 symbol_id 写入了同一天的行
        runtime_model.objects.using('kline').create(
            symbol_id=recreated.id,
            date=date(2024, 1, 2),
            open=Decimal('10.0'), high=Decimal('10.5'), low=Decimal('9.8'),
            close=Decimal('10.2'), volume=1000000, amount=Decimal('10200000'),
            adj_factor=Decimal('1.0'), turnover_rate=Decimal('0.5'),
        )
        self.assertEqual(
            runtime_model.objects.using('kline').filter(date=date(2024, 1, 2)).count(), 2
        )

        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-02'],
            '开盘': [10.0], '收盘': [10.2], '最高': [10.5], '最低': [9.8],
            '成交量': [1000000], '成交额': [10200000],
            '涨跌幅': [0.02], '涨跌额': [0.2], '换手率': [0.5],
        })

        added, _, error = sync_kline_for_symbol(
            recreated, start_date='2024-01-01', end_date='2024-01-05',
        )

        self.assertIsNone(error)
        # 重复行已收敛为一条，且孤儿行被清空
        self.assertEqual(
            runtime_model.objects.using('kline').filter(date=date(2024, 1, 2)).count(), 1
        )
        self.assertEqual(
            runtime_model.objects.using('kline').filter(symbol_id=orphan_symbol_id).count(), 0
        )
        self.assertEqual(added, 0)
        self.assertEqual(len(query_kline_table(
            recreated, date(2024, 1, 1), date(2024, 1, 5)
        )), 1)

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_does_not_rebind_rows_of_other_symbol(self, mock_hist):
        """认领只针对本分表：不得改挂其他标的的表，也不应把本表的行挂到别处。"""
        runtime_model = get_runtime_kline_model(self.symbol_a)
        runtime_model.objects.using('kline').create(
            symbol_id=self.symbol_a.id,
            date=date(2024, 1, 2),
            open=Decimal('10.0'), high=Decimal('10.5'), low=Decimal('9.8'),
            close=Decimal('10.2'), volume=1000000, amount=Decimal('10200000'),
            adj_factor=Decimal('1.0'), turnover_rate=Decimal('0.5'),
        )

        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-03'],
            '开盘': [10.0], '收盘': [10.3], '最高': [10.5], '最低': [9.8],
            '成交量': [1000000], '成交额': [10200000],
            '涨跌幅': [0.02], '涨跌额': [0.2], '换手率': [0.5],
        })
        added, _, error = sync_kline_for_symbol(
            self.symbol_a, start_date='2024-01-01', end_date='2024-01-10',
        )
        self.assertIsNone(error)
        self.assertEqual(added, 1)

        # 另一标的（不同 code → 不同分表）不受本次同步影响
        other = Symbol.objects.create(
            code='600000', name='浦发银行', market='A', exchange='SSE'
        )
        other_model = get_runtime_kline_model(other)
        self.assertEqual(
            other_model.objects.using('kline').filter(symbol_id__isnull=False).count(), 0
        )
        # 本表原有行未被改挂到其他 symbol
        self.assertEqual(
            runtime_model.objects.using('kline').filter(symbol_id=self.symbol_a.id).count(), 2
        )

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


def _tencent_unavailable(func):
    """固定走东财回退路径的标记装饰器。

    腾讯升为主源后，仅 mock 东财接口的用例会真的打到腾讯网络（结果不确定且很慢）。
    凡是用例**要断言东财通道自身行为**（代码补零、交易所前缀、列名规整）的，
    都必须显式钉在东财路径上，避免隐式依赖外网。
    """
    return patch(
        'apps.datasources.services.fetch_kline_from_ashare_tx',
        side_effect=Exception('tencent 主源不可用（测试固定走东财路径）'),
    )(func)


class MultiMarketKlineFetchTest(APITransactionTestCase):
    """港股/美股日线拉取（akshare 东财）与按市场分派防回归。

    背景：拉取层原先只实现 A 股（ashare/sina/腾讯），新增港股标的时
    `_normalize_ashare_code('00700')` 会把港股代码补零成 `000700` 当深市
    A 股拉取（错误行情静默入库）；美股代码 sina 不识别，永远返回空。
    修复后 HK/US 分派到港美股专用通道（腾讯为主源、东财为回退），
    并禁止再落入 A 股 ashare 通道。
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
        # 分表由运行期原生 SQL 创建（``kline_hk_00700`` 等），不属于任何 registered
        # model，故 Django 的 flush 看不到它——若不显式清空，上一个用例写入的行会残留到
        # 下一个用例。``sync_kline_for_symbol`` 现在会认领陈旧 symbol_id 的行，残留数据
        # 不再被忽略，从而让本类用例的added/skipped 断言互相干扰。
        for symbol in (self.symbol_hk, self.symbol_us, self.symbol_a_index):
            get_runtime_kline_model(symbol).objects.using('kline').all().delete()

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
    @_tencent_unavailable
    def test_fetch_hk_kline_via_akshare(self, mock_tx, mock_hist):
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
    @_tencent_unavailable
    def test_fetch_hk_short_code_padded_to_five(self, mock_tx, mock_hist):
        mock_hist.return_value = self._akshare_hist_df()
        symbol = Symbol.objects.create(code='700', name='腾讯', market='HK', exchange='HKEX')
        fetch_kline_from_ashare(symbol, '2024-01-01', '2024-01-31')
        self.assertEqual(mock_hist.call_args.kwargs['symbol'], '00700')

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_fetch_hk_empty_result_raises_located_error(self, mock_hist, mock_tx):
        # 两通道都无数据才报错：腾讯（主源）空 → 回退东财 → 东财也空 → 可定位 ValueError
        mock_hist.return_value = pd.DataFrame()
        mock_tx.side_effect = ValueError('腾讯通道未取到数据')
        with self.assertRaises(ValueError) as ctx:
            fetch_kline_from_ashare(self.symbol_hk, '2024-01-01', '2024-01-31')
        message = str(ctx.exception)
        self.assertIn('00700', message)
        # 报错须同时说明两条通道，便于从同步日志定位是哪一侧的问题
        self.assertIn('腾讯通道', message)
        self.assertIn('东财通道', message)
        mock_tx.assert_called_once()

    # ---- 美股 ----

    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    @_tencent_unavailable
    def test_fetch_us_kline_prefix_fallback(self, mock_tx, mock_hist):
        # 105(NASDAQ) 失败 → 106(NYSE) 命中
        mock_hist.side_effect = [Exception('not found'), self._akshare_hist_df()]
        df = fetch_kline_from_ashare(self.symbol_us, '2024-01-01', '2024-01-31', 'qfq')
        self.assertEqual(len(df), 2)
        self.assertEqual(mock_hist.call_count, 2)
        self.assertEqual(mock_hist.call_args_list[0].kwargs['symbol'], '105.AAPL')
        self.assertEqual(mock_hist.call_args_list[1].kwargs['symbol'], '106.AAPL')

    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    @_tencent_unavailable
    def test_fetch_us_explicit_prefix_used_directly(self, mock_tx, mock_hist):
        mock_hist.return_value = self._akshare_hist_df()
        symbol = Symbol.objects.create(code='105.TSLA', name='特斯拉', market='US', exchange='NASDAQ')
        fetch_kline_from_ashare(symbol, '2024-01-01', '2024-01-31')
        self.assertEqual(mock_hist.call_count, 1)
        self.assertEqual(mock_hist.call_args.kwargs['symbol'], '105.TSLA')

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    def test_fetch_us_all_prefixes_fail_raises_located_error(self, mock_hist, mock_tx):
        # 腾讯（主源）失败 → 回退东财 → 东财三前缀全失败 → 可定位 ValueError
        mock_hist.side_effect = Exception('connection down')
        mock_tx.side_effect = ValueError('腾讯通道未取到数据')
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
    @_tencent_unavailable
    def test_hk_us_never_routed_through_ashare(self, mock_tx, mock_get_price, mock_hk, mock_us):
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

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    def test_sync_hk_end_to_end_tencent_primary(self, mock_tx):
        """腾讯为主源的港股端到端入库：不 mock 任何远端 HTTP，只 mock 通道函数。"""
        mock_tx.return_value = pd.DataFrame({
            'date': ['2024-01-02', '2024-01-03'], 'open': [430.0, 432.0],
            'close': [432.0, 435.0], 'high': [439.0, 436.0], 'low': [431.0, 430.0],
            'volume': [18015236, 17000000], 'amount': [7.8e9, 7.4e9],
        })
        added, skipped, error = sync_kline_for_symbol(
            self.symbol_hk, 'daily',
            start_date='2024-01-01', end_date='2024-01-31', adjust='qfq',
        )
        self.assertIsNone(error)
        self.assertEqual(added, 2)
        rows = query_kline_table(self.symbol_hk, date(2024, 1, 1), date(2024, 1, 31))
        self.assertEqual(len(rows), 2)
        # 币种等市场元数据仍需正确落到 extra，不能因换通道丢失
        self.assertEqual(rows[0]['extra']['currency'], 'HKD')
        self.assertEqual(rows[0]['symbol'], '00700')
        self.assertEqual(rows[0]['close'], 432.0)
        # 重复同步应命中增量跳过，不重复写入
        added_again, skipped_again, error_again = sync_kline_for_symbol(
            self.symbol_hk, 'daily',
            start_date='2024-01-01', end_date='2024-01-31', adjust='qfq',
        )
        self.assertIsNone(error_again)
        self.assertEqual(added_again, 0)
        self.assertEqual(skipped_again, 2)

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    def test_sync_us_end_to_end_tencent_primary(self, mock_tx):
        """腾讯为主源的美股端到端入库。

        mock 必须返回**接近窗口交易日数**的行数：31 天窗口若只回2 根会被
        ``_is_suspect_thin_kline`` 判定为残缺并触发东财/新浪回退（进而联网），
        使本用例不再确定。真实美股每个交易日都有数据，用完整序列才符合前提。
        """
        dates = pd.bdate_range('2024-01-01', '2024-01-31')
        rows = len(dates)
        mock_tx.return_value = pd.DataFrame({
            'date': list(dates), 'open': [185.0] * rows, 'close': [185.64] * rows,
            'high': [186.5] * rows, 'low': [184.6] * rows,
            'volume': [58000000] * rows, 'amount': [1.07e10] * rows,
        })
        added, _, error = sync_kline_for_symbol(
            self.symbol_us, 'daily',
            start_date='2024-01-01', end_date='2024-01-31', adjust='qfq',
        )
        self.assertIsNone(error)
        self.assertEqual(added, rows)
        stored = query_kline_table(self.symbol_us, date(2024, 1, 1), date(2024, 1, 31))
        self.assertEqual(len(stored), rows)
        self.assertEqual(stored[0]['extra']['split_factor'], 1.0)
        self.assertEqual(stored[0]['symbol'], 'AAPL')

    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    @_tencent_unavailable
    def test_sync_new_hk_symbol_end_to_end(self, mock_tx, mock_hist):
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
    @_tencent_unavailable
    def test_sync_new_us_symbol_end_to_end(self, mock_tx, mock_hist):
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

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_sync_hk_failure_returns_located_error(self, mock_hist, mock_tx):
        # 同步层：两通道均失败时，added=0 且 error 可定位（此处必须 mock 腾讯通道，
        # 否则腾讯主源会打到真实网络，导致用例变慢且结果不确定）
        mock_hist.side_effect = Exception('network unreachable')
        mock_tx.side_effect = ValueError('腾讯通道未取到数据')
        added, _, error = sync_kline_for_symbol(
            self.symbol_hk, 'daily',
            start_date='2024-01-01', end_date='2024-01-31', adjust='qfq',
        )
        self.assertEqual(added, 0)
        self.assertIn('network unreachable', error or '')
        self.assertIn('腾讯通道', error or '')


class TencentChannelNormalizeTest(TestCase):
    """腾讯港美股通道：代码规整与成交额量纲（对齐腾讯真实返回结构）。"""

    def test_hk_code_zero_padded_to_five(self):
        # 实测：hk700 返回空数组，必须补零为 hk00700
        for raw in ['700', '00700', 'hk00700', 'HK$00700', '00700.HK']:
            self.assertEqual(_normalize_hk_code_tx(raw), 'hk00700', raw)
        self.assertEqual(_normalize_hk_code_tx('9988'), 'hk09988')

    def test_us_exchange_suffix_candidates(self):
        self.assertEqual(_us_exchange_candidates('AAPL'), ['AAPL.OQ', 'AAPL.N', 'AAPL.A'])
        self.assertEqual(_us_exchange_candidates('aapl'), ['AAPL.OQ', 'AAPL.N', 'AAPL.A'])
        # 已带后缀 → 不再回退
        self.assertEqual(_us_exchange_candidates('AAPL.OQ'), ['AAPL.OQ'])
        self.assertEqual(_us_exchange_candidates('usBABA.N'), ['BABA.N'])

    @patch('apps.datasources.ashare.requests.get')
    def test_hk_amount_converted_from_wan_yuan(self, mock_get):
        """港股行第 9 列是**万元**，需 ×1e4；否则成交额会缩小 1e4 倍。"""
        payload = {'code': 0, 'data': {'hk00700': {'qfqday': [
            ['2026-09-29', '439.4', '432.0', '439.4', '431.6', '18015236.0',
             {'cqr': '2026-09-29'}, '0.200', '780870.203'],
        ]}}}
        mock_get.return_value.json.return_value = payload
        df = get_price_day_tx_hk('700', count=1)
        self.assertAlmostEqual(float(df['amount'].iloc[0]), 780870.203 * 10000, places=2)

    @patch('apps.datasources.ashare.requests.get')
    def test_us_amount_not_scaled(self, mock_get):
        """美股成交额为**美元**（部分代码 11 列含该字段），系数必须为 1。"""
        payload = {'code': 0, 'data': {'usPDD.OQ': {'qfqday': [
            ['2026-09-30', '77.65', '77.94', '78.23', '77.29', '6191074',
             {}, '0.43', '481596571', '', '0.43'],
        ]}}}
        mock_get.return_value.json.return_value = payload
        df = get_price_day_tx_us('PDD', count=1)
        self.assertAlmostEqual(float(df['amount'].iloc[0]), 481596571.0, places=2)

    @patch('apps.datasources.ashare.requests.get')
    def test_row_without_amount_drops_column_for_fallback(self, mock_get):
        """6 列美股行无成交额 → 必须丢弃 amount 列，让上游 close*volume 兜底生效。

        留 NaN 会被上游 fillna(0) 归零，导致成交额整体丢失（实测 AAPL/BABA 为该形态）。
        """
        payload = {'code': 0, 'data': {'usAAPL.OQ': {'qfqday': [
            ['2026-09-30', '330.80', '333.02', '339.50', '330.14', '49988558.00'],
        ]}}}
        mock_get.return_value.json.return_value = payload
        df = get_price_day_tx_us('AAPL.OQ', count=1)
        self.assertNotIn('amount', df.columns)

    @patch('apps.datasources.ashare.requests.get')
    def test_us_tries_exchange_suffixes_until_data(self, mock_get):
        """接口对无效代码返回 200 + 空数组，只能靠后缀回退定位交易所。"""
        empty = {'code': 0, 'data': {'usXXX.OQ': {'day': []}}}
        hit = {'code': 0, 'data': {'usXXX.N': {'qfqday': [
            ['2026-09-30', '10.0', '10.5', '10.6', '9.9', '1000'],
        ]}}}
        mock_get.side_effect = [
            type('R', (), {'json': lambda s: empty, 'raise_for_status': lambda s: None})(),
            type('R', (), {'json': lambda s: hit, 'raise_for_status': lambda s: None})(),
        ]
        df = get_price_day_tx_us('XXX', count=1)
        self.assertEqual(len(df), 1)
        self.assertEqual(mock_get.call_count, 2)

    @patch('apps.datasources.ashare.requests.get')
    def test_us_all_suffixes_empty_raises_located_error(self, mock_get):
        empty = {'code': 0, 'data': {'usZZZ.OQ': {'day': []}}}
        mock_get.side_effect = [
            type('R', (), {'json': lambda s: empty, 'raise_for_status': lambda s: None})()
            for _ in range(3)
        ]
        with self.assertRaises(ValueError) as ctx:
            get_price_day_tx_us('ZZZ', count=1)
        self.assertIn('ZZZ', str(ctx.exception))


class TencentWindowFilterTest(TestCase):
    """腾讯通道忽略 start 参数，必须按请求窗口裁剪（否则窗口外历史行会污染分表）。"""

    def _frame(self, dates):
        return pd.DataFrame(
            [{'open': 10, 'close': 11, 'high': 12, 'low': 9, 'volume': 100, 'amount': 1100}
             for _ in dates],
            index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name=''),
        )

    def test_filters_outside_window(self):
        df = self._frame(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'])
        out = _tx_window_filter(df, date(2024, 1, 2), date(2024, 1, 3), 'US:TEST')
        self.assertEqual([ts.date().isoformat() for ts in out.index], ['2024-01-02', '2024-01-03'])

    def test_window_fully_outside_returns_empty(self):
        df = self._frame(['2024-01-01', '2024-01-02'])
        out = _tx_window_filter(df, date(2024, 3, 1), date(2024, 3, 5), 'US:TEST')
        self.assertTrue(out.empty)

    def test_empty_input_is_safe(self):
        empty = pd.DataFrame()
        self.assertTrue(_tx_window_filter(empty, date(2024, 1, 1), date(2024, 1, 2), 'X').empty)
        self.assertIsNone(_tx_window_filter(None, date(2024, 1, 1), date(2024, 1, 2), 'X'))


class TencentChannelDispatchTest(TestCase):
    """分派：腾讯主源 → 东财回退；A 股不受影响。"""

    def setUp(self):
        self.symbol_hk = Symbol.objects.create(
            code='00700', name='腾讯控股', market='HK', exchange='HKEX')

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_tencent_success_does_not_call_east_money(self, mock_hist, mock_tx):
        # 腾讯为主源：命中即返回，东财通道不应被打到（既省 akshare 依赖也省一次远端请求）
        #
        # 注意：mock 必须返回**接近窗口交易日数**的行数。原实现返回 1 根也能通过，
        # 但 31 天窗口只回 1 根正是 AGQ/SCO 的残缺形态——分派层现已按
        # 「非空但明显残缺」判定失败并回退东财（见 ``_is_suspect_thin_kline``），
        # 故此处改用完整数据，以继续验证「腾讯数据完整时不调东财」这一意图。
        dates = pd.bdate_range('2024-01-01', '2024-01-31')
        rows = len(dates)
        mock_tx.return_value = pd.DataFrame({
            'date': list(dates), 'open': [430.0] * rows, 'close': [432.0] * rows,
            'high': [439.0] * rows, 'low': [431.0] * rows,
            'volume': [18015236] * rows, 'amount': [7.8e9] * rows,
        })
        df = fetch_kline_from_ashare(self.symbol_hk, '2024-01-01', '2024-01-31')
        self.assertEqual(rows, len(df))
        mock_tx.assert_called_once()
        mock_hist.assert_not_called()

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_tencent_failure_falls_back_to_east_money(self, mock_hist, mock_tx):
        mock_tx.side_effect = Exception('tencent unreachable')
        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-02'], '开盘': [430.0], '收盘': [432.0], '最高': [439.0],
            '最低': [431.0], '成交量': [18015236], '成交额': [7.8e9],
        })
        df = fetch_kline_from_ashare(self.symbol_hk, '2024-01-01', '2024-01-31')
        self.assertEqual(len(df), 1)
        mock_tx.assert_called_once()
        mock_hist.assert_called_once()

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    @patch('apps.datasources.services.akshare_lib.stock_us_hist')
    def test_us_tencent_failure_falls_back_to_east_money(self, mock_hist, mock_tx):
        # 美股同样以腾讯为主源，且回退只落到美股东财接口（不误调港股接口）
        mock_tx.side_effect = Exception('tencent unreachable')
        mock_hist.return_value = pd.DataFrame({
            '日期': ['2024-01-02'], '开盘': [430.0], '收盘': [432.0], '最高': [439.0],
            '最低': [431.0], '成交量': [18015236], '成交额': [7.8e9],
        })
        symbol_us = Symbol.objects.create(
            code='AAPL', name='苹果', market='US', exchange='NASDAQ')
        df = fetch_kline_from_ashare(symbol_us, '2024-01-01', '2024-01-31')
        self.assertEqual(len(df), 1)
        mock_hist.assert_called_once()

    @patch('apps.datasources.services.fetch_kline_from_ashare_tx')
    @patch('apps.datasources.services.akshare_lib.stock_hk_hist')
    def test_both_channels_fail_error_names_both(self, mock_hist, mock_tx):
        mock_tx.side_effect = Exception('tencent unreachable')
        mock_hist.side_effect = Exception('east money rate limited')
        with self.assertRaises(ValueError) as ctx:
            fetch_kline_from_ashare(self.symbol_hk, '2024-01-01', '2024-01-31')
        message = str(ctx.exception)
        # 报错须同时说明两条通道，且先列主源腾讯，便于从同步日志定位是哪一侧的问题
        self.assertIn('腾讯通道', message)
        self.assertIn('东财通道', message)
        self.assertIn('tencent unreachable', message)
        self.assertIn('east money rate limited', message)

    def test_tencent_channel_rejects_a_share_market(self):
        symbol_a = Symbol.objects.create(
            code='600519', name='贵州茅台', market='A', exchange='SSE')
        with self.assertRaises(ValueError) as ctx:
            fetch_kline_from_ashare_tx(symbol_a, date(2024, 1, 1), date(2024, 1, 31))
        self.assertIn('仅支持港股/美股', str(ctx.exception))


class TencentUsSuffixCoverageTest(TestCase):
    """美股交易所后缀回退必须以「覆盖度」判定命中，而非「非空」。

    回归背景：AGQ / SCO（ProShares 反向杠杆 ETF，在 NYSE Arca 上市）此前只能拉到
    当天 1~2 根 K 线。原因是腾讯 ``usAGQ.OQ``（NASDAQ 通道）返回
    **200 + 非空数组但只有当天 1 根**，而回退逻辑判定「非空即返回」，
    于是永远不尝试 ``.A``(AMEX/NYSE Arca)——ETF 的历史数据在那里。
    常规股（NTSK / GOOG）首个候选即数据完整，因此该缺陷长期未暴露。
    """

    @staticmethod
    def _make_df(rows):
        import pandas as pd
        index = pd.to_datetime([date(2026, 9, 1) + timedelta(days=i) for i in range(rows)])
        return pd.DataFrame(
            {
                'time': index, 'open': [1.0] * rows, 'close': [1.0] * rows,
                'high': [1.0] * rows, 'low': [1.0] * rows,
                'volume': [1.0] * rows, 'amount': [1.0] * rows,
            },
            index=index,
        )

    def _patch(self, responses):
        """responses: {后缀: 行数}；记录每个被请求过的后缀。"""
        from apps.datasources import ashare as ashare_lib
        tried = []

        def fake_fetch(symbol, path, end_date='', unit='day', count=10,
                       fq='qfq', amount_unit=1.0):
            bare = symbol[len(ashare_lib._US_TX_PREFIX):]
            tried.append(bare)
            rows = responses.get(bare, 0)
            return self._make_df(rows) if rows else pd.DataFrame()

        original = ashare_lib._fetch_tx_market_daily
        ashare_lib._fetch_tx_market_daily = fake_fetch
        self.addCleanup(setattr, ashare_lib, '_fetch_tx_market_daily', original)
        return tried

    def test_etf_falls_through_to_arca_suffix_instead_of_returning_single_bar(self):
        """核心回归：首个候选仅 1 根时，必须继续试到 NYSE Arca 并取到完整历史。"""
        tried = self._patch({'AGQ.OQ': 1, 'AGQ.N': 1, 'AGQ.A': 300})
        df = get_price_day_tx_us('AGQ', count=300)
        self.assertEqual(300, len(df))
        self.assertEqual(['AGQ.OQ', 'AGQ.N', 'AGQ.A'], tried)

    def test_sco_etf_recovers_full_history(self):
        self._patch({'SCO.OQ': 1, 'SCO.N': 1, 'SCO.A': 300})
        self.assertEqual(300, len(get_price_day_tx_us('SCO', count=300)))

    def test_normal_stock_short_circuits_on_first_suffix(self):
        """常规股首个候选覆盖度达标时不应再多发请求（控制远端调用量）。"""
        tried = self._patch({'NTSK.OQ': 300, 'NTSK.N': 300, 'NTSK.A': 300})
        df = get_price_day_tx_us('NTSK', count=300)
        self.assertEqual(300, len(df))
        self.assertEqual(['NTSK.OQ'], tried)

    def test_sparse_candidates_degrade_to_best_attempt(self):
        """全部后缀都只有 1 根时，退化为最佳尝试而非抛错（避免新股/新股上市被误判失败）。"""
        self._patch({'ZZZ.OQ': 1, 'ZZZ.N': 1, 'ZZZ.A': 0})
        self.assertEqual(1, len(get_price_day_tx_us('ZZZ', count=300)))

    def test_all_suffixes_empty_raises_with_attempted_list(self):
        """全空时抛错，且错误信息列出所有已尝试后缀，便于从同步日志定位。"""
        self._patch({'QQQ.OQ': 0, 'QQQ.N': 0, 'QQQ.A': 0})
        with self.assertRaises(ValueError) as ctx:
            get_price_day_tx_us('QQQ', count=300)
        message = str(ctx.exception)
        self.assertIn('QQQ.OQ', message)
        self.assertIn('QQQ.N', message)
        self.assertIn('QQQ.A', message)

    def test_explicit_suffix_skips_other_exchanges(self):
        """库内代码已带后缀时不做多后缀试探（沿用既有约定）。"""
        from apps.datasources.ashare import _us_exchange_candidates
        self.assertEqual(['AAPL.OQ'], _us_exchange_candidates('AAPL.OQ'))
        self.assertEqual(['AAPL.OQ'], _us_exchange_candidates('usaapl.oq'))


class SinaUsChannelTest(TestCase):
    """新浪美股通道（``US_MinKService.getDailyK``）：字段转换与窗口裁剪。

    该通道存在的意义：腾讯与东财对 ``AGQ``/``SCO`` 等反向杠杆 ETF **没有历史数据**
    （腾讯仅当天 1 根且无复权序列），新浪则提供完整历史（实测 AGQ 自 2008-12-04 起
    4483 根），故作为第三级回退。
    """

    # 新浪原始返回：单字母字段 d/o/h/l/c/v/a，a 为美元成交额
    RAW = [
        {'d': '2026-09-29', 'o': '1.0', 'h': '2.0', 'l': '0.5', 'c': '1.5', 'v': '100', 'a': '150'},
        {'d': '2026-09-30', 'o': '1.5', 'h': '2.5', 'l': '1.0', 'c': '2.0', 'v': '200', 'a': '400'},
        {'d': '2026-10-01', 'o': '2.0', 'h': '3.0', 'l': '1.5', 'c': '2.5', 'v': '300', 'a': '750'},
        {'d': '2026-10-02', 'o': '2.5', 'h': '3.5', 'l': '2.0', 'c': '3.0', 'v': '400', 'a': '1200'},
    ]

    def _patch(self, payload):
        from apps.datasources import ashare as ashare_lib

        class FakeResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                if isinstance(self._data, Exception):
                    raise self._data
                return self._data

        original = ashare_lib.requests.get
        ashare_lib.requests.get = lambda *a, **k: FakeResponse(payload)
        self.addCleanup(setattr, ashare_lib.requests, 'get', original)

    def test_maps_sina_short_fields_to_internal_schema(self):
        """d/o/h/l/c/v/a → time/open/high/low/close/volume/amount，映射不可错位。"""
        self._patch(self.RAW)
        df = get_price_day_sina_us('AGQ')
        self.assertEqual(4, len(df))
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertTrue(df.index.is_monotonic_increasing)
        last = df.iloc[-1]
        self.assertEqual(2.5, last['open'])     # o
        self.assertEqual(3.5, last['high'])     # h
        self.assertEqual(2.0, last['low'])      # l
        self.assertEqual(3.0, last['close'])    # c
        self.assertEqual(400, last['volume'])   # v
        self.assertEqual(1200, last['amount'])  # a（美元，不换算）
        self.assertEqual(pd.Timestamp('2026-10-02'), df.index[-1])

    def test_accepts_prefixed_and_suffixed_codes(self):
        """库内代码可能带 ``us`` 前缀或 ``.OQ`` 后缀，均应归一化。"""
        from apps.datasources.ashare import _fetch_sina_symbol

        self.assertEqual('AGQ', _fetch_sina_symbol('AGQ'))
        self.assertEqual('AGQ', _fetch_sina_symbol('usAGQ'))
        self.assertEqual('AGQ', _fetch_sina_symbol('AGQ.OQ'))
        self.assertEqual('AGQ', _fetch_sina_symbol('  agq  '))
        self.assertEqual('', _fetch_sina_symbol(''))

    def test_count_limits_to_recent_rows(self):
        self._patch(self.RAW)
        df = get_price_day_sina_us('AGQ', count=2)
        self.assertEqual(2, len(df))
        self.assertEqual(pd.Timestamp('2026-10-01'), df.index[0])

    def test_amount_column_dropped_when_all_missing(self):
        """成交额整列缺失时应丢弃该列，交由上游 close*volume 兜底（否则会被归零）。"""
        raw = [{k: v for k, v in row.items() if k != 'a'} for row in self.RAW]
        self._patch(raw)
        df = get_price_day_sina_us('AGQ')
        self.assertNotIn('amount', df.columns)

    def test_empty_and_invalid_payload_returns_empty_frame(self):
        for payload in ([], None, {}, 'bad', ValueError('boom')):
            self._patch(payload)
            df = get_price_day_sina_us('AGQ')
            self.assertTrue(df.empty, f'payload={payload!r} 应返回空 DataFrame')

    def test_service_filters_by_requested_window(self):
        """新浪返回全部历史，必须按请求窗口裁剪，否则污染分表与增量判断。"""
        from apps.datasources.services import fetch_kline_from_sina_us
        from apps.watchlists.models import Symbol
        self._patch(self.RAW)
        symbol = Symbol.objects.create(code='AGQ', name='AGQ', market='US', exchange='')
        df = fetch_kline_from_sina_us(symbol, date(2026, 9, 30), date(2026, 10, 1))
        self.assertEqual(2, len(df))
        self.assertEqual(date(2026, 9, 30), df['date'].min())
        self.assertEqual(date(2026, 10, 1), df['date'].max())

    def test_service_raises_when_window_has_no_data(self):
        from apps.datasources.services import fetch_kline_from_sina_us
        from apps.watchlists.models import Symbol
        self._patch(self.RAW)
        symbol = Symbol.objects.create(code='AGQ', name='AGQ', market='US', exchange='')
        with self.assertRaises(ValueError) as ctx:
            fetch_kline_from_sina_us(symbol, date(2020, 1, 1), date(2020, 1, 31))
        self.assertIn('新浪通道', str(ctx.exception))

    def test_dispatch_falls_back_to_sina_when_tencent_and_east_money_fail(self):
        """端到端：腾讯残缺 + 东财不可达 → 采用新浪结果（AGQ/SCO 的修复路径）。"""
        from apps.datasources import services as svc
        from apps.watchlists.models import Symbol
        symbol = Symbol.objects.create(code='AGQ', name='AGQ', market='US', exchange='')
        self._patch(self.RAW)
        with mock.patch.object(svc, 'fetch_kline_from_ashare_tx',
                               return_value=self._pd(1)), \
                mock.patch.object(svc, '_fetch_kline_east_money',
                                  side_effect=ValueError('east money unreachable')):
            df = svc.fetch_kline_from_ashare(symbol, date(2026, 9, 28), date(2026, 10, 3))
        self.assertEqual(4, len(df))          # 新浪 4 根 > 腾讯 1 根
        self.assertEqual(3.0, df.iloc[-1]['close'])

    def _pd(self, rows):
        index = pd.date_range('2026-10-01', periods=rows, freq='D')
        return pd.DataFrame(
            {'date': [d.date() for d in index], 'open': [1.0] * rows, 'close': [1.0] * rows,
             'high': [1.0] * rows, 'low': [1.0] * rows,
             'volume': [1.0] * rows, 'amount': [1.0] * rows},
        )


class SuspectThinKlineTest(TestCase):
    """「非空但残缺」的 K 线必须被识别，并触发东财通道补全。

    回归背景：AGQ / SCO 重新同步后**仍只有 1 根**（此前的交易所后缀覆盖度修复无效，
    因为三个后缀都只返回 1 根）。真正的根因是：
    ``fetch_kline_from_ashare`` 过去**只在腾讯抛异常时**才回退东财，
    而腾讯对这类 ETF 返回 HTTP 200 + 非空数组（仅 1 根）、**不抛异常**，
    于是残缺数据被直接判定成功并入库，东财通道永远不会被调用。
    """

    WIDE = (date(2025, 12, 7), date(2026, 10, 3))     # 约 300 天，应有 ~215 根
    NARROW = (date(2026, 9, 30), date(2026, 10, 2))   # 3 天

    @staticmethod
    def _df(rows):
        import pandas as pd
        index = pd.date_range('2025-12-07', periods=rows, freq='D')
        return pd.DataFrame(
            {'date': index, 'open': [1.0] * rows, 'close': [1.0] * rows,
             'high': [1.0] * rows, 'low': [1.0] * rows,
             'volume': [1.0] * rows, 'amount': [1.0] * rows},
            index=index,
        )

    def test_single_bar_for_wide_window_is_suspect(self):
        """核心回归：300 天窗口只回 1 根，必须判定为残缺。"""
        self.assertTrue(_is_suspect_thin_kline(self._df(1), *self.WIDE))

    def test_healthy_row_count_is_not_suspect(self):
        self.assertFalse(_is_suspect_thin_kline(self._df(214), *self.WIDE))

    def test_partially_thin_is_suspect(self):
        """300 天只回 50 根同样属残缺（比例过低）。"""
        self.assertTrue(_is_suspect_thin_kline(self._df(50), *self.WIDE))

    def test_short_windows_not_flagged(self):
        """短窗口内的少量数据是正常的，不能误判（否则新股/当日同步会被拖累）。"""
        self.assertFalse(_is_suspect_thin_kline(self._df(3), *self.NARROW))
        single = date(2026, 10, 2)
        self.assertFalse(_is_suspect_thin_kline(self._df(1), single, single))

    def test_empty_is_not_suspect(self):
        """空结果交由原有「抛异常→回退」逻辑处理，不重复判定。"""
        self.assertFalse(_is_suspect_thin_kline(pd.DataFrame(), *self.WIDE))
        self.assertFalse(_is_suspect_thin_kline(None, *self.WIDE))

    def test_dispatch_prefers_east_money_when_tencent_thin(self):
        """端到端：腾讯 1 根 + 东财 214 根 → 采用东财结果。"""
        from apps.datasources import services as svc
        symbol = Symbol.objects.create(code='AGQ', name='AGQ', market='US', exchange='')
        em_calls = []

        def fake_em(*args, **kwargs):
            em_calls.append(1)
            return self._df(214)

        with mock.patch.object(svc, 'fetch_kline_from_ashare_tx', return_value=self._df(1)), \
                mock.patch.object(svc, '_fetch_kline_east_money', side_effect=fake_em):
            df = svc.fetch_kline_from_ashare(symbol, *self.WIDE)
        self.assertEqual(214, len(df))
        self.assertEqual(1, len(em_calls))

    def test_dispatch_keeps_tencent_result_when_all_fallbacks_unavailable(self):
        """东财与新浪**都**不可用时保留腾讯结果，不让整个同步失败。

        新浪接入后本用例必须同时屏蔽两个回退通道——否则新浪会真的联网取到数据
        （AGQ/SCO 在新浪有完整历史），掩盖「全部回退失效」的降级行为。
        """
        from apps.datasources import services as svc
        symbol = Symbol.objects.create(code='SCO', name='SCO', market='US', exchange='')
        with mock.patch.object(svc, 'fetch_kline_from_ashare_tx', return_value=self._df(1)), \
                mock.patch.object(svc, '_fetch_kline_east_money',
                                  side_effect=ValueError('east money unreachable')), \
                mock.patch.object(svc, 'fetch_kline_from_sina_us',
                                  side_effect=ValueError('sina unreachable')):
            df = svc.fetch_kline_from_ashare(symbol, *self.WIDE)
        self.assertEqual(1, len(df))

    def test_dispatch_does_not_call_east_money_when_tencent_healthy(self):
        """腾讯数据完整时不得多余调用东财（控制远端请求量）。"""
        from apps.datasources import services as svc
        symbol = Symbol.objects.create(code='NTSK', name='NTSK', market='US', exchange='')
        em_calls = []

        def fake_em(*args, **kwargs):
            em_calls.append(1)
            return self._df(214)

        with mock.patch.object(svc, 'fetch_kline_from_ashare_tx', return_value=self._df(214)), \
                mock.patch.object(svc, '_fetch_kline_east_money', side_effect=fake_em):
            df = svc.fetch_kline_from_ashare(symbol, *self.WIDE)
        self.assertEqual(214, len(df))
        self.assertEqual([], em_calls)
