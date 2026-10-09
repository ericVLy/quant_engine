from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .models import SetupState
from .setup import SetupAlreadyCompleted, complete_setup


User = get_user_model()


class UsersAPITest(APITestCase):
    def test_register_assigns_default_role(self):
        response = self.client.post('/api/users/register/', {
            'username': 'alice',
            'password': 'Strong-password-123',
            'password_confirm': 'Strong-password-123',
            'email': 'alice@example.com',
            'phone': '13800000000',
            'company': 'Quant Co',
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username='alice')
        self.assertTrue(user.check_password('Strong-password-123'))
        self.assertEqual(list(user.groups.values_list('name', flat=True)), ['user'])
        self.assertEqual(response.data['company'], 'Quant Co')

    def test_login_profile_update_and_logout(self):
        User.objects.create_user(username='alice', password='Strong-password-123')

        login_response = self.client.post('/api/users/login/', {
            'username': 'alice',
            'password': 'Strong-password-123',
        })
        self.assertEqual(login_response.status_code, status.HTTP_200_OK)

        profile_response = self.client.get('/api/users/profile/')
        self.assertEqual(profile_response.status_code, status.HTTP_200_OK)
        self.assertEqual(profile_response.data['username'], 'alice')

        update_response = self.client.patch('/api/users/profile/', {'company': 'New Co'})
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data['company'], 'New Co')

        logout_response = self.client.post('/api/users/logout/')
        self.assertEqual(logout_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            self.client.get('/api/users/profile/').status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_profile_requires_authentication(self):
        response = self.client.get('/api/users/profile/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_only_admin_can_manage_roles(self):
        user = User.objects.create_user(username='alice', password='Strong-password-123')
        self.client.login(username='alice', password='Strong-password-123')
        response = self.client.post(f'/api/users/{user.id}/roles/', {'roles': []})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_manage_roles(self):
        admin = User.objects.create_user(
            username='admin', password='Strong-password-123', is_staff=True
        )
        target = User.objects.create_user(username='alice', password='Strong-password-123')
        Group.objects.create(name='analyst')
        self.client.force_authenticate(admin)

        response = self.client.post(
            f'/api/users/{target.id}/roles/', {'roles': ['analyst']}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['roles'], ['analyst'])


class SetupGuideTest(APITestCase):
    """部署初始化引导：首次创建超级管理员，完成后永久关闭。"""

    def _payload(self, **overrides):
        payload = {
            'username': 'root',
            'password': 'Root-Strong-123',
            'password_confirm': 'Root-Strong-123',
            'email': 'root@example.com',
            'company': 'Quant Co',
        }
        payload.update(overrides)
        return payload

    def test_status_requires_setup_on_empty_database(self):
        response = self.client.get('/api/users/setup/status/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['setup_required'])

    def test_setup_creates_superuser_with_admin_role(self):
        response = self.client.post('/api/users/setup/', self._payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username='root')
        self.assertTrue(user.is_superuser, '首个用户必须是超级管理员')
        self.assertTrue(user.is_staff, '首个用户需能进入 Django admin')
        self.assertEqual(list(user.groups.values_list('name', flat=True)), ['admin'])
        self.assertEqual(user.company, 'Quant Co')
        self.assertTrue(user.check_password('Root-Strong-123'))

    def test_setup_logs_user_in_immediately(self):
        self.client.post('/api/users/setup/', self._payload())

        # 创建即登录：不应再要求用户手工输一次密码
        profile = self.client.get('/api/users/profile/')
        self.assertEqual(profile.status_code, status.HTTP_200_OK)
        self.assertEqual(profile.data['username'], 'root')

    def test_status_false_and_setup_rejected_after_completion(self):
        self.client.post('/api/users/setup/', self._payload())

        status_response = self.client.get('/api/users/setup/status/')
        self.assertFalse(status_response.data['setup_required'])

        again = self.client.post('/api/users/setup/', self._payload(username='intruder'))
        self.assertEqual(again.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(username='intruder').exists())
        self.assertEqual(User.objects.count(), 1, '不得创建第二个用户')

    def test_guide_stays_closed_even_after_all_users_deleted(self):
        """核心防后门：删光账号后引导也不得重新打开。

        若以「用户数是否为 0」判定，这里会重新开放引导，任何能触达该端点的人
        都能再次抢占超级管理员。
        """
        self.client.post('/api/users/setup/', self._payload())
        User.objects.all().delete()

        status_response = self.client.get('/api/users/setup/status/')
        self.assertFalse(
            status_response.data['setup_required'],
            '已完成初始化的系统，删号后不得重新开放引导',
        )
        again = self.client.post('/api/users/setup/', self._payload(username='intruder'))
        self.assertEqual(again.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(username='intruder').exists())

    def test_setup_closed_when_database_already_has_users(self):
        """已有用户的库（即便状态行缺失）也不暴露引导。"""
        User.objects.create_user(username='existing', password='Strong-password-123')

        status_response = self.client.get('/api/users/setup/status/')
        self.assertFalse(status_response.data['setup_required'])
        again = self.client.post('/api/users/setup/', self._payload())
        self.assertEqual(again.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(SETUP_ENABLED=False)
    def test_setup_rejected_when_env_switch_off(self):
        """SETUP_ENABLED=0 时即使空库也不可引导（生产加固开关）。"""
        status_response = self.client.get('/api/users/setup/status/')
        self.assertFalse(status_response.data['setup_required'])

        response = self.client.post('/api/users/setup/', self._payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.exists())

    def test_password_confirmation_must_match(self):
        response = self.client.post('/api/users/setup/', self._payload(password_confirm='Other-123456'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password_confirm', response.data)
        self.assertFalse(User.objects.exists())

    def test_weak_password_rejected(self):
        """管理员密码过弱必须拒绝——该账号拥有系统最高权限。"""
        response = self.client.post('/api/users/setup/', self._payload(
            password='12345678', password_confirm='12345678'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.exists())
        # 失败后引导仍应可用（未产生半成品状态）
        self.assertTrue(self.client.get('/api/users/setup/status/').data['setup_required'])

    def test_blank_username_rejected(self):
        response = self.client.post('/api/users/setup/', self._payload(username='   '))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.exists())

    def test_setup_state_is_singleton_and_precreated(self):
        """单行状态由迁移预建（并发首次初始化不应争抢 INSERT）。

        迁移预建 pk=1 是并发安全的前提：若首行留给运行时``get_or_create``，
        并发请求会同时 INSERT 同一 pk，在 SQLite 上抛 ``database table is locked``
        （表现为 500 而非正确的 403）。
        """
        state = SetupState.objects.get(pk=SetupState.SINGLETON_PK)
        self.assertFalse(state.completed)
        self.assertEqual(SetupState.objects.count(), 1)

        # 第二次 complete_setup 必须在事务内被拦住（并发防护的核心判定）
        complete_setup(username='first', password='Root-Strong-123')
        state.refresh_from_db()
        self.assertTrue(state.completed)
        self.assertEqual(state.completed_by, 'first')
        self.assertIsNotNone(state.completed_at)

        with self.assertRaises(SetupAlreadyCompleted):
            complete_setup(username='second', password='Root-Strong-123')
        self.assertFalse(User.objects.filter(username='second').exists())

    def test_concurrent_setup_creates_single_superuser(self):
        """并发提交只应产生一个超级管理员。

        **刻意不做真并发线程压测**（与 ``runner/tests_concurrency.py`` 同一取舍）：
        Django 测试库是**共享内存 SQLite**，多线程写会抛 ``database table is locked``
        而拿不到任何响应码，断言的是环境限制而非业务契约（且本项目执行链本就串行）。
        这里的并发不变量由上一例（事务内二次判定 + 预建单行）守住。

        真实文件库（WAL + busy_timeout=30）6 线程实测：
        ``[201, 403, 403, 403, 403, 403]``、超级管理员 1 个、用户总数 1 个。
        """
        # 串行模拟「第二个请求在第一个已完成后到达」
        first = self.client.post('/api/users/setup/', self._payload())
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.client.post('/api/users/setup/', self._payload(username='intruder'))
        self.assertEqual(second.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
        self.assertEqual(User.objects.count(), 1)
