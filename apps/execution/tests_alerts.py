"""
告警功能测试文件
"""
import logging
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.cases.models import Case
from apps.execution.models import (
    Alert, AlertChannel, SuiteRun, Order, ExecutionLog,
    AccountFundConfig, FundAllocation
)
from apps.execution.alerts import (
    AlertService, AlertSeverity, AlertType, alert_service
)

User = get_user_model()

# 禁用告警服务的邮件发送，避免测试时发送真实邮件
@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
    EMAIL_HOST='',
)
class AlertServiceTests(TestCase):
    """告警服务测试"""
    
    def setUp(self):
        """设置测试数据"""
        # 创建测试用户
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123'
        )
        
        # 先创建测试策略
        self.suite = Suite.objects.create(
            name='测试策略',
            aggregate_method='weighted_sum',
            status='published',
            created_by=self.user
        )
        
        # 创建测试计划（需要先创建 Suite）
        self.plan = Plan.objects.create(
            name='测试计划',
            root_suite=self.suite,
            trigger_type='time',
            cron_expr='0 9 * * 1-5',
            symbol_scope={'type': 'all'},
            status='published',
            created_by=self.user
        )
        
        # 创建测试策略执行
        self.suite_run = SuiteRun.objects.create(
            plan=self.plan,
            suite=self.suite,
            symbol='000001',
            status='failed',
            started_at=timezone.now(),
            ended_at=timezone.now()
        )
        
        # 创建测试执行日志
        self.execution_log = ExecutionLog.objects.create(
            plan=self.plan,
            symbol='000001',
            trigger_time=timezone.now(),
            final_direction=1,
            status='failed',
            error_msg='测试错误消息'
        )
        
        # 创建测试订单
        self.order = Order.objects.create(
            log=self.execution_log,
            symbol='000001',
            direction='buy',
            price=10.5,
            volume=100,
            status='rejected',
            last_error='订单被拒绝'
        )
        
        # 清空告警渠道缓存
        alert_service.reload_channels()
    
    def test_create_alert(self):
        """测试创建告警"""
        alert = alert_service.create_alert(
            alert_type=AlertType.ORDER_FAILED,
            title='测试订单失败告警',
            message='订单 000001 买入 100股 失败',
            severity=AlertSeverity.HIGH,
            plan=self.plan,
            order=self.order,
            error_code='ORDER_REJECTED'
        )
        
        self.assertEqual(alert.alert_type, AlertType.ORDER_FAILED)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)
        self.assertEqual(alert.title, '测试订单失败告警')
        self.assertEqual(alert.order, self.order)
        self.assertEqual(alert.error_code, 'ORDER_REJECTED')
        self.assertEqual(alert.status, 'pending')
        self.assertFalse(alert.in_app_notified)
        self.assertFalse(alert.email_notified)
    
    def test_create_order_failed_alert(self):
        """测试创建订单失败告警"""
        alert = alert_service.create_order_failed_alert(
            order=self.order,
            error_message='资金不足',
            error_code='INSUFFICIENT_FUNDS',
            severity=AlertSeverity.CRITICAL
        )
        
        self.assertEqual(alert.alert_type, AlertType.ORDER_FAILED)
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL)
        self.assertIn('订单执行失败', alert.title)
        self.assertIn('资金不足', alert.message)
        self.assertEqual(alert.error_code, 'INSUFFICIENT_FUNDS')
        self.assertEqual(alert.order, self.order)
    
    def test_create_suite_failed_alert(self):
        """测试创建策略执行失败告警"""
        alert = alert_service.create_suite_failed_alert(
            suite_run=self.suite_run,
            error_message='策略执行超时',
            error_code='TIMEOUT',
            severity=AlertSeverity.HIGH
        )
        
        self.assertEqual(alert.alert_type, AlertType.SUITE_FAILED)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)
        self.assertIn('策略执行失败', alert.title)
        self.assertIn('策略执行超时', alert.message)
        self.assertEqual(alert.error_code, 'TIMEOUT')
        self.assertEqual(alert.suite_run, self.suite_run)
    
    def test_create_risk_violation_alert(self):
        """测试创建风控违规告警"""
        alert = alert_service.create_risk_violation_alert(
            plan=self.plan,
            violation_message='超过单日交易金额上限',
            violation_type='每日限额',
            severity=AlertSeverity.CRITICAL
        )
        
        self.assertEqual(alert.alert_type, AlertType.RISK_VIOLATION)
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL)
        self.assertIn('风控违规', alert.title)
        self.assertIn('超过单日交易金额上限', alert.message)
        self.assertEqual(alert.plan, self.plan)
    
    def test_create_system_error_alert(self):
        """测试创建系统错误告警"""
        alert = alert_service.create_system_error_alert(
            error_message='数据库连接失败',
            error_code='DB_CONNECTION_ERROR',
            severity=AlertSeverity.MEDIUM
        )
        
        self.assertEqual(alert.alert_type, AlertType.SYSTEM_ERROR)
        self.assertEqual(alert.severity, AlertSeverity.MEDIUM)
        self.assertIn('系统错误', alert.title)
        self.assertIn('数据库连接失败', alert.message)
        self.assertEqual(alert.error_code, 'DB_CONNECTION_ERROR')
    
    def test_alert_channel_filtering(self):
        """测试告警渠道过滤"""
        # 创建告警渠道配置
        high_severity_channel = AlertChannel.objects.create(
            channel_type='in_app',
            is_enabled=True,
            min_severity='high'
        )
        
        # 测试高严重程度告警应该通过
        high_alert = alert_service.create_alert(
            alert_type=AlertType.ORDER_FAILED,
            title='高严重程度告警',
            message='测试',
            severity=AlertSeverity.HIGH
        )
        self.assertTrue(high_severity_channel.should_send_alert(high_alert))
        
        # 测试低严重程度告警不应该通过
        low_alert = alert_service.create_alert(
            alert_type=AlertType.SYSTEM_ERROR,
            title='低严重程度告警',
            message='测试',
            severity=AlertSeverity.LOW
        )
        self.assertFalse(high_severity_channel.should_send_alert(low_alert))
    
    def test_alert_channel_type_filtering(self):
        """测试告警类型过滤"""
        # 创建只接收订单失败告警的渠道
        order_failed_channel = AlertChannel.objects.create(
            channel_type='in_app',
            is_enabled=True,
            min_severity='low',
            alert_types=['order_failed']
        )
        
        # 测试订单失败告警应该通过
        order_alert = alert_service.create_alert(
            alert_type=AlertType.ORDER_FAILED,
            title='订单失败告警',
            message='测试',
            severity=AlertSeverity.HIGH
        )
        self.assertTrue(order_failed_channel.should_send_alert(order_alert))
        
        # 测试策略失败告警不应该通过
        suite_alert = alert_service.create_alert(
            alert_type=AlertType.SUITE_FAILED,
            title='策略失败告警',
            message='测试',
            severity=AlertSeverity.HIGH
        )
        self.assertFalse(order_failed_channel.should_send_alert(suite_alert))
    
    def test_in_app_notification(self):
        """测试应用内通知"""
        # 创建启用应用内通知的渠道
        AlertChannel.objects.create(
            channel_type='in_app',
            is_enabled=True,
            min_severity='low'
        )
        
        alert = alert_service.create_alert(
            alert_type=AlertType.ORDER_FAILED,
            title='测试通知',
            message='测试消息',
            severity=AlertSeverity.HIGH
        )
        
        # 重新加载对象以检查通知状态
        alert.refresh_from_db()
        self.assertTrue(alert.in_app_notified)
        self.assertFalse(alert.email_notified)
    
    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
        EMAIL_HOST='localhost',
        EMAIL_PORT=1025,
        EMAIL_USE_TLS=False,
        EMAIL_HOST_USER='',
        EMAIL_HOST_PASSWORD='',
        DEFAULT_FROM_EMAIL='noreply@example.com'
    )
    def test_email_notification(self):
        """测试邮件通知"""
        # 创建启用邮件通知的渠道
        AlertChannel.objects.create(
            channel_type='email',
            is_enabled=True,
            min_severity='high',
            email_recipients=['admin@example.com', 'trader@example.com'],
            email_subject_prefix='[量化系统]'
        )
        
        # 创建高严重程度告警
        alert = alert_service.create_alert(
            alert_type=AlertType.ORDER_FAILED,
            title='测试邮件通知',
            message='测试邮件消息',
            severity=AlertSeverity.HIGH,
            send_notifications=True,
        )
        
        # 重新加载对象以检查通知状态
        alert.refresh_from_db()
        notified = alert.email_notified
        print(f"----Email notified: {notified}, error: {alert.notification_error}----")
        self.assertTrue(alert.email_notified, f"邮件通知失败: {alert.notification_error}")
        
        # 检查邮件是否已发送
        # 注意：由于使用了控制台邮件后端，邮件会被记录在 mail.outbox
        # self.assertEqual(len(mail.outbox), 1)
        
        # 检查邮件内容
        # self.assertEqual(mail.outbox[0].subject, "[量化系统] [HIGH] 测试邮件通知")
        # self.assertIn("测试邮件消息", mail.outbox[0].body)
        # self.assertIn('[量化系统]', mail.outbox[0].subject)
        # self.assertIn('测试邮件通知', mail.outbox[0].subject)
        # self.assertEqual(len(mail.outbox[0].to), 2)


class AlertModelTests(TestCase):
    """告警模型测试"""
    
    def setUp(self):
        """设置测试数据"""
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # 先创建测试策略
        self.suite = Suite.objects.create(
            name='测试策略',
            aggregate_method='weighted_sum',
            status='published',
            created_by=self.user
        )
        
        self.plan = Plan.objects.create(
            name='测试计划',
            root_suite=self.suite,
            trigger_type='time',
            cron_expr='0 9 * * 1-5',
            symbol_scope={'type': 'all'},
            status='published',
            created_by=self.user
        )
    
    def test_alert_creation(self):
        """测试告警创建"""
        alert = Alert.objects.create(
            alert_type='order_failed',
            severity='high',
            title='测试告警',
            message='测试消息',
            plan=self.plan
        )
        
        self.assertEqual(alert.status, 'pending')
        self.assertFalse(alert.in_app_notified)
        self.assertFalse(alert.email_notified)
        self.assertEqual(str(alert), '[HIGH] 测试告警')
    
    def test_alert_acknowledgment(self):
        """测试告警确认"""
        alert = Alert.objects.create(
            alert_type='order_failed',
            severity='high',
            title='测试告警',
            message='测试消息',
            plan=self.plan
        )
        
        # 确认告警
        alert.status = 'acknowledged'
        alert.acknowledged_by = self.user
        alert.acknowledged_at = timezone.now()
        alert.save()
        
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'acknowledged')
        self.assertEqual(alert.acknowledged_by, self.user)
        self.assertIsNotNone(alert.acknowledged_at)
    
    def test_alert_resolution(self):
        """测试告警解决"""
        alert = Alert.objects.create(
            alert_type='order_failed',
            severity='high',
            title='测试告警',
            message='测试消息',
            plan=self.plan
        )
        
        # 解决告警
        alert.status = 'resolved'
        alert.resolved_by = self.user
        alert.resolved_at = timezone.now()
        alert.save()
        
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'resolved')
        self.assertEqual(alert.resolved_by, self.user)
        self.assertIsNotNone(alert.resolved_at)
    
    def test_alert_channel_creation(self):
        """测试告警渠道创建"""
        channel = AlertChannel.objects.create(
            channel_type='email',
            is_enabled=True,
            min_severity='medium',
            email_recipients=['admin@example.com']
        )
        
        self.assertEqual(channel.channel_type, 'email')
        self.assertTrue(channel.is_enabled)
        self.assertEqual(channel.email_recipients, ['admin@example.com'])
        self.assertEqual(str(channel), '邮件通知 (启用)')


class AlertAPITests(APITestCase):
    """告警 API 测试"""
    
    def setUp(self):
        """设置测试数据"""
        self.client = APIClient()
        
        # 创建测试用户
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # 先创建测试策略
        self.suite = Suite.objects.create(
            name='测试策略',
            aggregate_method='weighted_sum',
            status='published',
            created_by=self.user
        )
        
        # 创建测试告警
        self.plan = Plan.objects.create(
            name='测试计划',
            root_suite=self.suite,
            trigger_type='time',
            cron_expr='0 9 * * 1-5',
            symbol_scope={'type': 'all'},
            status='published',
            created_by=self.user
        )
        
        self.alert1 = Alert.objects.create(
            alert_type='order_failed',
            severity='high',
            title='告警1',
            message='消息1',
            plan=self.plan,
            status='pending'
        )
        
        self.alert2 = Alert.objects.create(
            alert_type='suite_failed',
            severity='critical',
            title='告警2',
            message='消息2',
            plan=self.plan,
            status='pending'
        )
    
    def test_list_alerts_requires_auth(self):
        """测试列表告警需要认证"""
        response = self.client.get('/api/execution/alerts/')
        # DRF 仅配置 SessionAuthentication 时，未认证请求返回 403 (PermissionDenied)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    
    def test_list_alerts_authenticated(self):
        """测试认证用户可以列表告警"""
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/execution/alerts/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
    
    def test_alert_statistics(self):
        """测试告警统计"""
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/execution/alerts/statistics/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('overview', response.data)
        self.assertIn('by_type', response.data)
        self.assertEqual(response.data['overview']['total'], 2)
        self.assertEqual(response.data['overview']['pending'], 2)
    
    def test_acknowledge_alert(self):
        """测试确认告警"""
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            f'/api/execution/alerts/{self.alert1.id}/actions/',
            {'action': 'acknowledge', 'note': '已查看'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # 检查告警状态
        self.alert1.refresh_from_db()
        self.assertEqual(self.alert1.status, 'acknowledged')
        self.assertEqual(self.alert1.acknowledged_by, self.user)
    
    def test_resolve_alert(self):
        """测试解决告警"""
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            f'/api/execution/alerts/{self.alert1.id}/actions/',
            {'action': 'resolve', 'note': '已解决'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # 检查告警状态
        self.alert1.refresh_from_db()
        self.assertEqual(self.alert1.status, 'resolved')
        self.assertEqual(self.alert1.resolved_by, self.user)
    
    def test_invalid_alert_action(self):
        """测试无效的告警操作"""
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            f'/api/execution/alerts/{self.alert1.id}/actions/',
            {'action': 'invalid_action'}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AlertIntegrationTests(TestCase):
    """告警集成测试"""
    
    def setUp(self):
        """设置测试数据"""
        # 创建用户
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # 创建账户资金配置
        self.account_config = AccountFundConfig.objects.create(
            account_id='test_account',
            total_capital=100000.00
        )
        
        # 先创建测试策略
        self.suite = Suite.objects.create(
            name='测试策略',
            aggregate_method='weighted_sum',
            status='published',
            created_by=self.user
        )
        
        # 创建计划
        self.plan = Plan.objects.create(
            name='测试计划',
            root_suite=self.suite,
            trigger_type='time',
            cron_expr='0 9 * * 1-5',
            symbol_scope={'type': 'all'},
            account_id='test_account',
            allocated_capital=50000.00,
            status='published',
            created_by=self.user
        )
        
        # 设置告警渠道
        AlertChannel.objects.create(
            channel_type='in_app',
            is_enabled=True,
            min_severity='low'
        )
        
        alert_service.reload_channels()
    
    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
        EMAIL_HOST='',
    )
    def test_order_failure_creates_alert(self):
        """测试订单失败自动创建告警"""
        # 创建执行日志
        execution_log = ExecutionLog.objects.create(
            plan=self.plan,
            symbol='000001',
            trigger_time=timezone.now(),
            final_direction=1,
            status='failed',
            error_msg='资金不足'
        )
        
        # 创建失败订单
        order = Order.objects.create(
            log=execution_log,
            symbol='000001',
            direction='buy',
            price=10.5,
            volume=1000,
            status='rejected',
            last_error='资金不足'
        )
        
        # 模拟订单失败时创建告警
        alert = alert_service.create_order_failed_alert(
            order=order,
            error_message='订单被拒绝：资金不足',
            error_code='INSUFFICIENT_FUNDS'
        )
        
        # 验证告警创建
        self.assertEqual(alert.alert_type, AlertType.ORDER_FAILED)
        self.assertEqual(alert.order, order)
        self.assertEqual(alert.plan, self.plan)
        self.assertIn('资金不足', alert.message)
        
        # 验证应用内通知已发送
        self.assertTrue(alert.in_app_notified)
    
    def test_risk_violation_creates_alert(self):
        """测试风控违规自动创建告警"""
        # 模拟风控违规
        alert = alert_service.create_risk_violation_alert(
            plan=self.plan,
            violation_message='超过单日交易金额上限：当前100000，限额80000',
            violation_type='每日限额',
            severity=AlertSeverity.CRITICAL
        )
        
        # 验证告警创建
        self.assertEqual(alert.alert_type, AlertType.RISK_VIOLATION)
        self.assertEqual(alert.plan, self.plan)
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL)
        self.assertIn('超过单日交易金额上限', alert.message)
        
        # 验证应用内通知已发送
        self.assertTrue(alert.in_app_notified)


# 禁用日志输出，避免测试时日志干扰
logging.disable(logging.CRITICAL)