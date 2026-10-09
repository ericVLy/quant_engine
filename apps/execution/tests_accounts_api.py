"""账户预配置 API（gm user id 预配置 → 资金/持仓快照 → Plan 按账户匹配）。

覆盖 ``/api/execution/accounts/`` 的读写权限分离（管理员可写/登录用户只读）、
资金字段只读、停用前置校验，以及 Plan 资金占用按预配置账户匹配的失败路径。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.execution.models import AccountFundConfig
from apps.plans.models import Plan
from apps.suites.models import Suite

User = get_user_model()
ACCOUNT = 'gm-user-preconfig-001'


class AccountApiTest(TestCase):
    databases = ['default', 'kline']

    def setUp(self):
        self.admin = User.objects.create_superuser('acc_admin', password='Admin-Strong-123')
        self.member = User.objects.create_user('acc_member', password='Member-Strong-123')
        self.client = APIClient()

    def test_list_requires_login(self):
        # DRF 的 SessionAuthentication 在无凭证时返回 403（不带 WWW-Authenticate 头）
        self.assertEqual(
            self.client.get('/api/execution/accounts/').status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_member_can_read_but_not_write(self):
        """读取任意登录用户；写入仅管理员（N-05：账户配置含半敏感账户 ID）。"""
        self.client.force_login(user=self.member)
        self.assertEqual(self.client.get('/api/execution/accounts/').status_code, status.HTTP_200_OK)
        create = self.client.post(
            '/api/execution/accounts/', {'account_id': ACCOUNT}, format='json')
        self.assertEqual(create.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_preconfigure_account(self):
        self.client.force_login(user=self.admin)
        resp = self.client.post('/api/execution/accounts/', {
            'account_id': ACCOUNT, 'display_name': '主账户',
            'capital_basis': 'cash', 'is_active': True,
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.display_name, '主账户')
        self.assertEqual(cfg.capital_basis, 'cash')

    def test_account_id_is_required(self):
        self.client.force_login(user=self.admin)
        resp = self.client.post('/api/execution/accounts/',
                                {'account_id': '  '}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_fund_fields_are_read_only(self):
        """资金/持仓快照只能由 gm 同步写入，前端提交一律忽略。"""
        self.client.force_login(user=self.admin)
        resp = self.client.post('/api/execution/accounts/', {
            'account_id': ACCOUNT, 'total_capital': '99999999',
            'available_cash': '88888', 'position_count': 99,
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertNotEqual(cfg.total_capital, Decimal('99999999'))
        self.assertEqual(cfg.position_count, 0)

    def test_cannot_deactivate_account_still_referenced_by_plan(self):
        """仍有 Plan 引用时不允许停用（否则这些 Plan 的额度校验会悬空）。"""
        suite = Suite.objects.create(name='s-deactivate')
        cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('100000'))
        Plan.objects.create(name='p1', account_id=ACCOUNT, root_suite=suite,
                            allocated_capital=Decimal('1000'))
        self.client.force_login(user=self.admin)
        resp = self.client.patch(f'/api/execution/accounts/{cfg.id}/',
                                 {'is_active': False}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        cfg.refresh_from_db()
        self.assertTrue(cfg.is_active)

    def test_sync_requires_gm_channel(self):
        """未配置 GM_TOKEN 时给出可定位提示，而非 500。"""
        cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('100000'))
        self.client.force_login(user=self.admin)
        with patch('apps.execution.views.build_gm_broker_from_settings',
                   side_effect=RuntimeError('未配置 GM_TOKEN，无法连接 gm 终端')):
            resp = self.client.post(f'/api/execution/accounts/{cfg.id}/sync/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('GM_TOKEN', str(resp.data))

    def test_sync_uses_channel_and_persists_snapshot(self):
        cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('1'))
        broker = type('B', (), {
            'account_id': None,
            'get_account': lambda self: {
                'balance': 80000, 'market_value': 20000, 'available': 75000},
            'get_positions': lambda self: [
                {'symbol': 'SHSE.600000', 'volume': 100, 'closep': 10.5,
                 'market_value': 1050}],
        })()
        self.client.force_login(user=self.admin)
        with patch('apps.execution.views.build_gm_broker_from_settings', return_value=broker):
            resp = self.client.post(f'/api/execution/accounts/{cfg.id}/sync/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        cfg.refresh_from_db()
        self.assertEqual(str(cfg.total_capital), '100000.00')
        self.assertEqual(cfg.position_count, 1)

    def test_account_id_is_masked_in_response(self):
        """N-05：响应里的账户 ID 走脱敏字段，不回显完整值。"""
        cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('100000'))
        self.client.force_login(user=self.admin)
        resp = self.client.get(f'/api/execution/accounts/{cfg.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('masked_account_id', resp.data)
        self.assertNotEqual(resp.data['masked_account_id'], ACCOUNT)


class PlanMatchesPreconfiguredAccountTest(TestCase):
    """Plan 的资金占用按预配置 gm user id 匹配。"""

    databases = ['default', 'kline']

    def setUp(self):
        self.user = User.objects.create_superuser('plan_admin', password='Admin-Strong-123')
        self.client = APIClient()
        self.client.force_login(user=self.user)
        self.cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('100000'))
        self.suite = Suite.objects.create(name='root-suite')

    def _plan_payload(self, **overrides):
        payload = {'name': 'p', 'trigger_type': 'manual', 'suite_start_mode': 'manual',
                   'root_suite': self.suite.id, 'account_id': ACCOUNT,
                   'allocated_capital': '1000'}
        payload.update(overrides)
        return payload

    def test_unconfigured_account_rejected(self):
        resp = self.client.post('/api/plans/', self._plan_payload(
            account_id='never-preconfigured'), format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('allocated_capital', resp.data)
        self.assertIn('未预配置', str(resp.data['allocated_capital'][0]))

    def test_inactive_account_rejected(self):
        self.cfg.is_active = False
        self.cfg.save()
        resp = self.client.post('/api/plans/', self._plan_payload(), format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('停用', str(resp.data['allocated_capital'][0]))

    def test_over_allocation_rejected(self):
        resp = self.client.post('/api/plans/', self._plan_payload(
            allocated_capital='99999999'), format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('空闲资金', str(resp.data['allocated_capital'][0]))

    def test_valid_plan_created_and_status_returned(self):
        resp = self.client.post('/api/plans/', self._plan_payload(), format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        status_info = resp.data['account_status']
        self.assertTrue(status_info['configured'])
        self.assertTrue(status_info['is_active'])
        # available_capital =总额 − 已占用；本用例新建的 Plan 占用 1000
        self.assertEqual(status_info['available_capital'], '99000.00')
