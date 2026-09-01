import logging
from django.test import TestCase
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from decimal import Decimal
from datetime import date, datetime, timedelta
import pandas as pd
from unittest.mock import patch

from apps.watchlists.models import Symbol
from apps.datasources.models import (
    DataSource, RealtimeSnapshot, KLineSyncLog,
    AStockKLine, HKStockKLine, USStockKLine
)
from apps.datasources.services import (
    get_kline_model, sync_kline_for_symbol, sync_all_symbols,
    get_kline_table_name, query_kline_table
)

logger = logging.getLogger(__name__)


class DataSourceAPITest(APITestCase):
    """测试数据源配置 CRUD 接口"""

    def setUp(self):
        logger.info("=== DataSourceAPITest 开始 ===")
        self.client = APIClient()
        self.list_url = '/api/datasources/sources/'
        self.data = {
            'name': '测试数据源',
            'source_type': 'akshare',
            'endpoint': 'https://example.com',
            'auth_info': {'token': 'test'},
            'priority': 1,
            'is_active': True
        }
        logger.info("准备测试数据源配置")

    def test_create_datasource(self):
        logger.info("测试创建数据源")
        response = self.client.post(self.list_url, self.data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DataSource.objects.count(), 1)
        ds = DataSource.objects.first()
        self.assertEqual(ds.name, '测试数据源')
        logger.info(f"数据源创建成功: {ds.name} (ID: {ds.id})")

    def test_list_datasources(self):
        logger.info("测试列出所有数据源")
        ds1 = DataSource.objects.create(name='源1', source_type='akshare')
        ds2 = DataSource.objects.create(name='源2', source_type='tushare')
        logger.info(f"已创建两个数据源: {ds1.name}, {ds2.name}")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        logger.info("列表返回记录数: %d", len(response.data))

    def test_retrieve_datasource(self):
        logger.info("测试获取单个数据源")
        ds = DataSource.objects.create(name='源1', source_type='akshare')
        logger.info(f"创建数据源 ID: {ds.id}")
        url = f'/api/datasources/sources/{ds.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], '源1')
        logger.info("成功获取数据源详情")

    def test_update_datasource(self):
        logger.info("测试更新数据源")
        ds = DataSource.objects.create(name='旧名', source_type='akshare')
        logger.info(f"原数据源: {ds.name}")
        url = f'/api/datasources/sources/{ds.id}/'
        response = self.client.put(url, {'name': '新名', 'source_type': 'tushare'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ds.refresh_from_db()
        self.assertEqual(ds.name, '新名')
        logger.info(f"更新后名称: {ds.name}")

    def test_delete_datasource(self):
        logger.info("测试删除数据源")
        ds = DataSource.objects.create(name='删除测试', source_type='akshare')
        logger.info(f"待删除数据源 ID: {ds.id}")
        url = f'/api/datasources/sources/{ds.id}/'
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(DataSource.objects.count(), 0)
        logger.info("删除成功")


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
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['symbol']['code'], '000001')
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
        self.assertEqual(len(response.data), 1)
        logger.info("日志列表返回记录数: 1")

    def test_retrieve_log(self):
        logger.info("测试获取单个同步日志")
        url = f'/api/datasources/sync-logs/{self.log.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['symbol']['code'], '000002')
        logger.info("成功获取同步日志详情")


class KLineAPITest(APITestCase):
    """测试 K 线查询和同步接口"""
    databases = ['default', 'kline']

    def setUp(self):
        logger.info("=== KLineAPITest 开始 ===")
        self.client = APIClient()
        self.symbol = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        # 预置 K 线数据，日期从 2024-01-10 开始
        for i in range(1, 6):
            AStockKLine.objects.create(
                symbol=self.symbol,
                date=date(2024, 1, 10 + i),
                open=Decimal('10.0') + Decimal(i),
                high=Decimal('10.5') + Decimal(i),
                low=Decimal('9.5') + Decimal(i),
                close=Decimal('10.2') + Decimal(i),
                volume=1000000 * i,
                amount=Decimal('1000000') * i,
                adj_factor=Decimal('1.0'),
                turnover_rate=Decimal('0.5') * i
            )
        logger.info(f"预置 5 条 K 线数据，日期 2024-01-11 至 2024-01-15")
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
        self.assertEqual(len(response.data), 5)
        first = response.data[0]
        self.assertIn('symbol', first)
        self.assertIn('date', first)
        self.assertIn('extra', first)
        self.assertEqual(str(first['extra']['adj_factor']), '1.000000')
        logger.info(f"查询成功，返回 {len(response.data)} 条记录")

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
        self.assertEqual(AStockKLine.objects.count(), 7)
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


class ServicesTest(TestCase):
    """测试数据服务函数"""
    databases = ['default', 'kline']

    def setUp(self):
        logger.info("=== ServicesTest 开始 ===")
        self.symbol_a = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        logger.info(f"创建测试标的: {self.symbol_a.code}")

    def test_get_kline_model(self):
        logger.info("测试获取 K 线模型类")
        model = get_kline_model(self.symbol_a)
        self.assertEqual(model, AStockKLine)
        logger.info(f"返回模型: {model.__name__}")

    def test_dynamic_kline_table_name_and_query(self):
        logger.info("测试按股票编码创建动态分表和查询")
        table_name = get_kline_table_name(self.symbol_a)
        self.assertEqual(table_name, 'kline_a_000001')
        self.assertTrue(table_name.startswith('kline_'))

        AStockKLine.objects.create(
            symbol=self.symbol_a,
            date=date(2024, 1, 5),
            open=Decimal('10.1'),
            high=Decimal('10.8'),
            low=Decimal('9.9'),
            close=Decimal('10.6'),
            volume=2000000,
            amount=Decimal('20000000'),
            adj_factor=Decimal('1.0')
        )

        rows = query_kline_table(self.symbol_a, date(2024, 1, 5), date(2024, 1, 5))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['symbol'], '000001')
        self.assertEqual(str(rows[0]['close']), '10.6000')
        logger.info(f"动态分表查询返回 {len(rows)} 条，表名为 {table_name}")

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
        self.assertTrue(AStockKLine.objects.filter(symbol=self.symbol_a, date=date(2024,1,1)).exists())
        logger.info(f"新增 {added} 条，跳过 {skipped} 条")

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_for_symbol_skip_existing(self, mock_hist):
        logger.info("测试同步已存在的数据（应跳过）")
        # 先创建一条已有数据
        AStockKLine.objects.create(
            symbol=self.symbol_a,
            date=date(2024, 1, 1),
            open=Decimal('10.0'),
            high=Decimal('10.5'),
            low=Decimal('9.8'),
            close=Decimal('10.2'),
            volume=1000000,
            amount=Decimal('10200000'),
            adj_factor=Decimal('1.0')
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
        logger.info(f"新增 {added} 条，跳过 {skipped} 条")

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