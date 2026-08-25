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
from apps.datasources.services import get_kline_model, sync_kline_for_symbol, sync_all_symbols


class DataSourceAPITest(APITestCase):
    """测试数据源配置 CRUD 接口"""

    def setUp(self):
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

    def test_create_datasource(self):
        response = self.client.post(self.list_url, self.data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DataSource.objects.count(), 1)
        ds = DataSource.objects.first()
        self.assertEqual(ds.name, '测试数据源')

    def test_list_datasources(self):
        DataSource.objects.create(name='源1', source_type='akshare')
        DataSource.objects.create(name='源2', source_type='tushare')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_retrieve_datasource(self):
        ds = DataSource.objects.create(name='源1', source_type='akshare')
        url = f'/api/datasources/sources/{ds.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], '源1')

    def test_update_datasource(self):
        ds = DataSource.objects.create(name='旧名', source_type='akshare')
        url = f'/api/datasources/sources/{ds.id}/'
        response = self.client.put(url, {'name': '新名', 'source_type': 'tushare'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ds.refresh_from_db()
        self.assertEqual(ds.name, '新名')

    def test_delete_datasource(self):
        ds = DataSource.objects.create(name='删除测试', source_type='akshare')
        url = f'/api/datasources/sources/{ds.id}/'
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(DataSource.objects.count(), 0)


class RealtimeSnapshotAPITest(APITestCase):
    """测试实时快照只读接口"""

    def setUp(self):
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

    def test_list_snapshots(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['symbol']['code'], '000001')

    def test_retrieve_snapshot_by_symbol_id(self):
        url = f'/api/datasources/snapshots/{self.symbol.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['symbol']['code'], '000001')
        self.assertEqual(response.data['price'], '12.3400')


class KLineSyncLogAPITest(APITestCase):
    """测试同步日志只读接口"""

    def setUp(self):
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

    def test_list_logs(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_retrieve_log(self):
        url = f'/api/datasources/sync-logs/{self.log.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['symbol']['code'], '000002')


class KLineAPITest(APITestCase):
    """测试 K 线查询和同步接口"""

    def setUp(self):
        self.client = APIClient()
        self.symbol = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        # 预置一些 K 线数据，日期从 2024-01-10 开始，避免与测试同步日期冲突
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

        self.query_url = '/api/datasources/kline/query/'
        self.sync_url = '/api/datasources/kline/sync/'

    def test_query_kline_success(self):
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

    def test_query_kline_missing_params(self):
        response = self.client.get(self.query_url, {'symbol': '000001'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('start 和 end 日期必填', response.data['detail'])

    def test_query_kline_symbol_not_found(self):
        response = self.client.get(self.query_url, {
            'symbol': '999999',
            'start': '2024-01-01',
            'end': '2024-01-05'
        })
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_single(self, mock_hist):
        # mock 返回 2024-01-01 和 2024-01-02 的数据，这两个日期不存在于预置数据中
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
        self.assertEqual(AStockKLine.objects.count(), 7)  # 原有5 + 新增2
        log = KLineSyncLog.objects.latest('created_at')
        self.assertEqual(log.records_added, 2)
        self.assertEqual(log.status, 'success')

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_all(self, mock_hist):
        # mock 返回单日数据 2024-01-01，该日期不存在
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

        response = self.client.post(self.sync_url, {
            'symbol': 'all',
            'start_date': '2024-01-01',
            'end_date': '2024-01-01'
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)  # 只有一个 A 股标的
        result = response.data['results'][0]
        self.assertEqual(result['symbol'], '000001')
        self.assertEqual(result['added'], 1)


class ServicesTest(TestCase):
    """测试数据服务函数"""

    def setUp(self):
        self.symbol_a = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        # 港股和美股不创建，避免影响测试
        # self.symbol_hk = ...
        # self.symbol_us = ...

    def test_get_kline_model(self):
        self.assertEqual(get_kline_model(self.symbol_a), AStockKLine)

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_for_symbol_new_data(self, mock_hist):
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

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_kline_for_symbol_skip_existing(self, mock_hist):
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

    @patch('apps.datasources.services.ak.stock_zh_a_hist')
    def test_sync_all_symbols(self, mock_hist):
        # 只创建一个 A 股标的，确保只处理 A 股
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