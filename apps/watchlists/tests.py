import logging
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from unittest.mock import patch
import pandas as pd

from .models import Symbol, Group, Watchlist
from .services import resolve_symbol_scope, sync_market_data

# 配置日志
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

User = get_user_model()


class SymbolAPITest(APITestCase):
    """测试标的管理接口"""

    def setUp(self):
        logger.info("=== SymbolAPITest 开始 ===")
        self.client = APIClient()
        self.symbol1 = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE'
        )
        self.symbol2 = Symbol.objects.create(
            code='600036', name='招商银行', market='A', exchange='SSE'
        )
        self.list_url = '/api/watchlists/symbols/'
        logger.info(f"已创建两个标的: {self.symbol1.code}, {self.symbol2.code}")

    def test_list_symbols(self):
        logger.info("测试列出所有标的")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        logger.info(f"返回 {len(response.data)} 条记录")

    def test_search_symbol(self):
        logger.info("测试搜索标的（按名称）")
        response = self.client.get(self.list_url, {'search': '平安'})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['code'], '000001')
        logger.info(f"搜索到标的: {response.data[0]['code']}")

    def test_create_symbol(self):
        logger.info("测试创建新标的")
        data = {'code': '000002', 'name': '万科A', 'market': 'A', 'exchange': 'SZSE'}
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Symbol.objects.count(), 3)
        logger.info("创建成功")

    def test_update_symbol(self):
        logger.info("测试更新标的信息")
        url = f'/api/watchlists/symbols/{self.symbol1.id}/'
        data = {'name': '平安银行新'}
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.symbol1.refresh_from_db()
        self.assertEqual(self.symbol1.name, '平安银行新')
        logger.info(f"更新后名称: {self.symbol1.name}")

    def test_delete_symbol(self):
        logger.info("测试删除标的")
        url = f'/api/watchlists/symbols/{self.symbol1.id}/'
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Symbol.objects.count(), 1)
        logger.info("删除成功")


class GroupAPITest(APITestCase):
    """测试分组管理接口"""

    def setUp(self):
        logger.info("=== GroupAPITest 开始 ===")
        self.client = APIClient()
        self.symbol1 = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.symbol2 = Symbol.objects.create(code='600036', name='招商银行', market='A')
        self.group = Group.objects.create(name='蓝筹')
        self.group.symbols.add(self.symbol1, self.symbol2)
        self.list_url = '/api/watchlists/groups/'
        logger.info(f"创建分组 '{self.group.name}'，包含 2 个标的")

    def test_list_groups(self):
        logger.info("测试列出所有分组")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['symbols'][0]['code'], '000001')
        logger.info("分组列表返回记录数: 1")

    def test_add_symbols_to_group(self):
        logger.info("测试向分组添加标的")
        new_symbol = Symbol.objects.create(code='000003', name='B股', market='A')
        url = f'/api/watchlists/groups/{self.group.id}/add-symbols/'
        response = self.client.post(url, {'symbol_ids': [new_symbol.id]})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['added'], 1)
        self.assertIn(new_symbol, self.group.symbols.all())
        logger.info(f"添加标的 {new_symbol.code} 成功")

    def test_remove_symbols_from_group(self):
        logger.info("测试从分组移除标的")
        url = f'/api/watchlists/groups/{self.group.id}/remove-symbols/'
        response = self.client.post(url, {'symbol_ids': [self.symbol1.id]})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['removed'], 1)
        self.assertNotIn(self.symbol1, self.group.symbols.all())
        logger.info(f"移除标的 {self.symbol1.code} 成功")


class WatchlistAPITest(APITestCase):
    """测试用户自选池接口"""

    def setUp(self):
        logger.info("=== WatchlistAPITest 开始 ===")
        self.user = User.objects.create_user(username='testuser', password='12345')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.symbol1 = Symbol.objects.create(code='000001', name='平安银行', market='A')
        self.symbol2 = Symbol.objects.create(code='600036', name='招商银行', market='A')
        self.group1 = Group.objects.create(name='蓝筹')
        self.group1.symbols.add(self.symbol1, self.symbol2)
        self.group2 = Group.objects.create(name='科技')
        self.watchlist_url = '/api/watchlists/watchlist/'
        logger.info(f"用户 {self.user.username} 已认证")

    def test_get_watchlist(self):
        logger.info("测试获取用户自选池（自动创建）")
        response = self.client.get(self.watchlist_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user'], self.user.id)
        self.assertEqual(response.data['groups'], [])
        logger.info("自选池获取成功")

    def test_update_watchlist(self):
        logger.info("测试更新用户自选池（绑定分组）")
        data = {'group_ids': [self.group1.id, self.group2.id]}
        response = self.client.put(self.watchlist_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['groups']), 2)
        logger.info(f"已绑定 {len(response.data['groups'])} 个分组")

    def test_watchlist_auto_create(self):
        logger.info("测试 GET 自动创建 Watchlist")
        response = self.client.get(self.watchlist_url)
        self.assertEqual(Watchlist.objects.count(), 1)
        logger.info("Watchlist 自动创建成功")


class ServicesTest(TestCase):
    def setUp(self):
        logger.info("=== ServicesTest 开始 ===")
        self.symbol1 = Symbol.objects.create(code='000001', name='A')
        self.symbol2 = Symbol.objects.create(code='600036', name='B')
        self.group1 = Group.objects.create(name='g1')
        self.group1.symbols.add(self.symbol1, self.symbol2)
        self.group2 = Group.objects.create(name='g2')
        self.group2.symbols.add(self.symbol1)
        logger.info(f"创建 2 个标、2 个分组")

    def test_resolve_symbol_scope_all(self):
        logger.info("测试解析 symbol_scope: all")
        scope = {'type': 'all'}
        qs = resolve_symbol_scope(scope)
        self.assertEqual(qs.count(), 2)
        logger.info(f"返回 {qs.count()} 个标的")

    def test_resolve_symbol_scope_groups(self):
        logger.info("测试解析 symbol_scope: groups")
        scope = {'type': 'groups', 'group_ids': [self.group1.id]}
        qs = resolve_symbol_scope(scope)
        self.assertEqual(qs.count(), 2)
        logger.info(f"返回 {qs.count()} 个标的")

    def test_resolve_symbol_scope_symbols(self):
        logger.info("测试解析 symbol_scope: symbols")
        scope = {'type': 'symbols', 'symbol_codes': ['000001']}
        qs = resolve_symbol_scope(scope)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().code, '000001')
        logger.info(f"返回标的: {qs.first().code}")

    @patch('akshare.stock_info_a_code_name')
    def test_sync_market_data(self, mock_ak):
        """
        测试同步功能：
        1. 先清空 Symbol 表，验证新增（created）
        2. 再次调用，验证更新（updated）
        """
        logger.info("测试同步全市场标的（mock AkShare）")

        # 准备 mock 数据
        df = pd.DataFrame({
            'code': ['000001', '600036'],
            'name': ['平安银行', '招商银行']
        })
        mock_ak.return_value = df

        # ----- 场景1：新增 -----
        # 清空现有 Symbol（不影响其他测试，因为测试事务会回滚）
        Symbol.objects.all().delete()
        result = sync_market_data()
        self.assertEqual(result['created'], 2)
        self.assertEqual(result['updated'], 0)
        # 验证数据已写入
        self.assertEqual(Symbol.objects.filter(market='A').count(), 2)
        self.assertEqual(Symbol.objects.get(code='000001').name, '平安银行')
        self.assertEqual(Symbol.objects.get(code='600036').name, '招商银行')

        # ----- 场景2：更新 -----
        # 第二次调用，数据已存在，应执行更新
        result2 = sync_market_data()
        self.assertEqual(result2['created'], 0)
        self.assertEqual(result2['updated'], 2)
        # 验证数据未被重复创建
        self.assertEqual(Symbol.objects.filter(market='A').count(), 2)

        logger.info("新增/更新测试通过")