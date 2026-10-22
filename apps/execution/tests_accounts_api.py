"""账户预配置 API（gm user id 预配置 → 资金/持仓快照 → Plan 按账户匹配）。

覆盖 ``/api/execution/accounts/`` 的读写权限分离（管理员可写/登录用户只读）、
资金字段只读、停用前置校验，以及 Plan 资金占用按预配置账户匹配的失败路径。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
import os
import subprocess
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
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
        """未配置 GM_TOKEN 时给出可定位提示，而非 500（不必起子进程）。"""
        cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('100000'))
        self.client.force_login(user=self.admin)
        with override_settings(GM_TOKEN=''):
            with patch.dict('os.environ', {}, clear=False):
                os.environ.pop('GM_TOKEN', None)
                resp = self.client.post(f'/api/execution/accounts/{cfg.id}/sync/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('GM_TOKEN', str(resp.data))

    def test_sync_delegates_to_subprocess_and_returns_fresh_snapshot(self):
        """同步经子进程执行，成功后回读数据库返回最新快照。"""
        cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('1'))
        self.client.force_login(user=self.admin)
        seen = {}

        def fake_sync(account_id, capital_basis='total'):
            seen['account_id'] = account_id
            seen['capital_basis'] = capital_basis
            AccountFundConfig.objects.filter(pk=cfg.pk).update(
                total_capital=Decimal('100000.00'), position_count=1)
            return {'ok': True, 'detail': ''}

        with override_settings(GM_TOKEN='fake-token'):
            with patch('apps.execution.views.run_account_sync_in_subprocess',
                       side_effect=fake_sync):
                resp = self.client.post(f'/api/execution/accounts/{cfg.id}/sync/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(seen['account_id'], ACCOUNT)
        self.assertEqual(str(resp.data['account']['total_capital']), '100000.00')
        self.assertEqual(resp.data['account']['position_count'], 1)

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


class SyncEndpointNeverHangsTest(TestCase):
    """端到端保证：gm 终端关闭时，同步接口**必定快速返回 4xx**，不会挂住。

    实测（真实 gm SDK + 本地黑洞端口，终端不可达）：
    - ``GmBrokerAdapter`` 构造（``set_serv_addr`` + ``set_token`` + ``set_account_id``）约 0.15s；
    - 而 ``get_cash()`` **持有 GIL 一直阻塞**，同进程内的心跳线程完全停摆，
      主线程连 ``join(timeout)`` 的超时逻辑都执行不到——
      即**任何"同进程加超时"的方案都无效**，整个 Web 进程会被冻结。

    因此同步必须经**子进程**隔离：卡住时 ``kill()`` 掉子进程即可，
    Web 进程毫发无损。这组用例把该约束钉死。
    """

    databases = ['default', 'kline']

    def setUp(self):
        self.admin = User.objects.create_superuser('hang_admin', password='Admin-Strong-123')
        self.client = APIClient()
        self.client.force_login(user=self.admin)
        self.cfg = AccountFundConfig.objects.create(
            account_id=ACCOUNT, total_capital=Decimal('100000'))

    def test_sync_returns_400_instead_of_hanging(self):
        """子进程被kill 后，接口必须快速返回可读的 4xx。"""
        started = time.monotonic()
        with override_settings(GM_TOKEN='fake-token'):
            with patch('apps.execution.views.subprocess.run',
                       side_effect=subprocess.TimeoutExpired(cmd='sync', timeout=35)):
                resp = self.client.post(f'/api/execution/accounts/{self.cfg.id}/sync/')
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('gm 终端无响应', str(resp.data.get('detail', '')))
        self.assertLess(elapsed, 15, '同步接口必须在超时内返回，不能挂住')

    def test_sync_must_not_call_gm_inline(self):
        """回归：sync 动作**绝不能**在 Web 进程内直接调用 gm SDK。

        gm SDK 阻塞时持有 GIL，内联调用会冻结整个进程（整站打不开），
        同进程内的超时保护根本不会被执行到。
        """
        import inspect

        from apps.execution import views as execution_views

        src = inspect.getsource(execution_views.AccountFundConfigViewSet.sync)
        self.assertNotIn('sync_account_funds(', src,
                         'sync 动作不得内联调用 sync_account_funds')
        self.assertNotIn('GmBrokerAdapter(', src,
                         'sync 动作不得在 Web 进程内构造 gm 适配器')
        self.assertIn('run_account_sync_in_subprocess', src)

    def test_site_still_usable_after_failed_sync(self):
        """同步失败后，其它接口必须照常可用（这是本次的核心诉求）。"""
        with override_settings(GM_TOKEN='fake-token'):
            with patch('apps.execution.views.subprocess.run',
                       side_effect=subprocess.TimeoutExpired(cmd='sync', timeout=35)):
                self.client.post(f'/api/execution/accounts/{self.cfg.id}/sync/')
        # 失败后：列表可读、可再写、原有数据未被清零
        self.assertEqual(self.client.get('/api/execution/accounts/').status_code,
                         status.HTTP_200_OK)
        create = self.client.post('/api/execution/accounts/',
                                  {'account_id': 'still-usable'}, format='json')
        self.assertEqual(create.status_code, status.HTTP_201_CREATED)

    # --------------------------------------------------- 管道 EOF 回归
    def test_sync_subprocess_env_forces_updater_off(self):
        """子进程环境**必须强制**关掉分时更新器（不能靠 setdefault）。

        回归（2026-10-22）：Web 进程自己的环境里
        ``MONITORING_UPDATER_ENABLED`` 已是 '1'，``setdefault`` 对已存在的
        键不生效 → 子进程照样拉起分时采样子进程 → 采样器继承
        ``capture_output`` 的管道 → 父进程退出后 EOF 永不到达 →
        ``subprocess.run`` 一直等到超时 → 报"gm 终端无响应"，**而 gm 完全正常**。
        """
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout='{}', stderr='')

        # 模拟 Web 进程的真实环境：更新器是开的
        with patch.dict(os.environ, {'MONITORING_UPDATER_ENABLED': '1'}, clear=False), \
                patch('apps.execution.views.subprocess.run', _fake_run):
            with override_settings(GM_TOKEN='fake-token'):
                self.client.post(f'/api/execution/accounts/{self.cfg.id}/sync/')

        env = captured.get('env') or {}
        self.assertEqual(env.get('MONITORING_UPDATER_ENABLED'), '0',
                         '子进程环境必须强制关闭分时更新器，不能受父环境影响')
        self.assertEqual(env.get('FUNDAMENTALS_ENABLED'), '0',
                         '子进程环境必须强制关闭基本面刷新')

    def test_sync_source_does_not_rely_on_setdefault_for_updater(self):
        """源码层面钉死：``MONITORING_UPDATER_ENABLED`` 必须直接赋值。

        ``env.setdefault(...)`` 对父环境已存在的键无效，是本 bug 的直接原因。
        """
        import inspect

        from apps.execution import views as execution_views

        src = inspect.getsource(execution_views.run_account_sync_in_subprocess)
        self.assertNotIn("setdefault('MONITORING_UPDATER_ENABLED'", src,
                         '分时更新器开关不能�� setdefault（父环境里通常已是 1）')
        self.assertIn("env['MONITORING_UPDATER_ENABLED'] = '0'", src)
        self.cfg.refresh_from_db()
        self.assertEqual(str(self.cfg.total_capital), '100000.00',
                         '失败绝不写 0：额度应保持上次成功值')
