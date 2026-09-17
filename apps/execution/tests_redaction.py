"""
N-05 专项测试：PII 脱敏与日志卫生

覆盖（关联 documents.md 5.1.2「敏感配置保护设计」、N-04、N-05）：
- redaction 脱敏纯函数（邮箱 / 手机号 / 账户与订单 ID / 异常栈折叠 / 通用文本）
- RedactionLogFilter 日志过滤器（防御性兜底：args 折叠、格式化异常静默放行）
- 告警通知链路脱敏（邮件正文折叠异常栈；日志不出现收件人/账户明文）
- 告警渠道 API 权限分级（IsAdminUser）与序列化器字段白名单
"""
import logging

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.execution.alerts import AlertService
from apps.execution.models import AlertChannel
from apps.execution.redaction import (
    RedactionLogFilter,
    mask_account_id,
    mask_email,
    mask_phone,
    redact_text,
    strip_traceback,
)

User = get_user_model()

UUID_SAMPLE = 'efd94fdb-1234-5678-9012-abcdef123456'
HEX_SECRET = '90a06e71d167d48c5471c9d56e781a69'
EMAIL_SAMPLE = 'trader@example.com'
PHONE_SAMPLE = '13800138000'


class RedactionFunctionTests(TestCase):
    """脱敏纯函数单元测试"""

    def test_mask_email_keeps_prefix_and_domain(self):
        self.assertEqual(mask_email(EMAIL_SAMPLE), 'tr****@example.com')

    def test_mask_email_keeps_single_char(self):
        self.assertEqual(mask_email('a@example.com'), 'a****@example.com')

    def test_mask_email_leaves_text_without_email(self):
        self.assertEqual(mask_email('无邮箱的普通文本'), '无邮箱的普通文本')

    def test_mask_phone(self):
        self.assertEqual(mask_phone(PHONE_SAMPLE), '138****8000')

    def test_mask_phone_inside_text(self):
        self.assertEqual(
            mask_phone(f'联系 {PHONE_SAMPLE} 处理'),
            '联系 138****8000 处理',
        )

    def test_mask_account_id_uuid_keeps_head4_tail4(self):
        self.assertEqual(mask_account_id(UUID_SAMPLE), 'efd9****3456')

    def test_mask_account_id_long_non_uuid(self):
        self.assertEqual(mask_account_id('ABCDEFGHIJ'), 'ABCD****IJ')

    def test_mask_account_id_short_fully_masked(self):
        self.assertEqual(mask_account_id('abc123'), '****')
        self.assertEqual(mask_account_id(''), '')

    def test_strip_traceback_folds_stack(self):
        text = (
            '同步失败\n'
            'Traceback (most recent call last):\n'
            '  File "C:/app/secret.py", line 10, in run\n'
            'ValueError: bad token\n'
        )
        out = strip_traceback(text)
        self.assertIn('同步失败', out)
        self.assertIn('[异常栈已折叠]', out)
        self.assertNotIn('Traceback (most recent call last)', out)
        self.assertNotIn('secret.py', out)

    def test_strip_traceback_keeps_plain_text(self):
        self.assertEqual(strip_traceback('普通错误消息'), '普通错误消息')

    def test_redact_text_masks_all_pii_types(self):
        text = (
            f'用户 {EMAIL_SAMPLE}（{PHONE_SAMPLE}）账户 {UUID_SAMPLE} '
            f'使用密钥 {HEX_SECRET}'
        )
        out = redact_text(text)
        self.assertNotIn(EMAIL_SAMPLE, out)
        self.assertNotIn(PHONE_SAMPLE, out)
        self.assertNotIn(UUID_SAMPLE, out)
        self.assertNotIn(HEX_SECRET, out)
        self.assertIn('tr****@example.com', out)
        self.assertIn('138****8000', out)
        self.assertIn('efd9****3456', out)

    def test_redact_text_plain_unchanged(self):
        plain = '运行日志：K线同步完成，共 120 条'
        self.assertEqual(redact_text(plain), plain)


def _make_record(msg, args=None):
    return logging.LogRecord(
        name='test.logger',
        level=logging.INFO,
        pathname='test_path.py',
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


class RedactionLogFilterTests(TestCase):
    """logging 过滤器兜底测试"""

    def test_masks_pii_in_message(self):
        record = _make_record(f'下单用户 {EMAIL_SAMPLE} 触发风控')
        self.assertTrue(RedactionLogFilter().filter(record))
        self.assertNotIn(EMAIL_SAMPLE, record.msg)
        self.assertIn('tr****@example.com', record.msg)

    def test_folds_args_before_redaction(self):
        record = _make_record('用户 %s 下单失败', args=(EMAIL_SAMPLE,))
        RedactionLogFilter().filter(record)
        self.assertNotIn(EMAIL_SAMPLE, record.getMessage())
        self.assertIn('tr****@example.com', record.msg)
        self.assertIsNone(record.args)

    def test_swallows_format_errors_and_never_blocks(self):
        # '%d %s' + (1,) 会让 getMessage() 抛 TypeError，过滤器必须静默放行
        record = _make_record('%d %s', args=(1,))
        self.assertTrue(RedactionLogFilter().filter(record))
        self.assertEqual(record.msg, '%d %s')

    def test_plain_message_untouched(self):
        record = _make_record('同步完成，共 120 条')
        RedactionLogFilter().filter(record)
        self.assertEqual(record.msg, '同步完成，共 120 条')


class AlertNotificationRedactionTests(TestCase):
    """告警通知链路脱敏集成测试：邮件正文不夹带 PII 明文与异常栈"""

    def setUp(self):
        self.service = AlertService()
        self.channel = AlertChannel.objects.create(
            channel_type='email',
            is_enabled=True,
            email_recipients=['ops@example.com'],
            min_severity='low',
        )

    def _make_alert(self, message):
        return self.service.create_alert(
            alert_type='system_error',
            severity='high',
            title='系统错误',
            message=message,
            send_notifications=True,
        )

    @override_settings(EMAIL_HOST='localhost', EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_email_body_redacts_pii_and_traceback(self):
        message = (
            '账户 efd94fdb-1234-5678-9012-abcdef123456 下单失败，'
            '联系 13800138000，密钥 90a06e71d167d48c5471c9d56e781a69\n'
            'Traceback (most recent call last):\n'
            '  File "C:/app/secret.py", line 1\n'
            'ValueError: bad token'
        )
        alert = self._make_alert(message)
        self.assertTrue(alert.email_notified)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertNotIn('efd94fdb-1234-5678-9012-abcdef123456', body)
        self.assertNotIn('13800138000', body)
        self.assertNotIn('90a06e71d167d48c5471c9d56e781a69', body)
        self.assertNotIn('Traceback (most recent call last)', body)
        self.assertIn('efd9****3456', body)
        self.assertIn('[异常栈已折叠]', body)

    @override_settings(EMAIL_HOST='localhost', EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_notification_error_stored_redacted(self):
        # 收件人含 PII，发送失败路径下 notification_error 不含明文
        with __import__('unittest').mock.patch(
            'apps.execution.alerts.send_mail', side_effect=Exception('connect 13800138000 refused')
        ):
            alert = self._make_alert('普通错误消息')
        self.assertFalse(alert.email_notified)
        self.assertIn('138****8000', alert.notification_error)
        self.assertNotIn('13800138000', alert.notification_error)


class AlertChannelPermissionTests(APITestCase):
    """告警渠道 API 权限分级：渠道配置含收件人 PII，仅管理员可读写"""

    def setUp(self):
        self.list_url = '/api/execution/alert-channels/'
        self.staff = User.objects.create_user('admin1', password='x', is_staff=True)
        self.plain = User.objects.create_user('plain1', password='x')
        AlertChannel.objects.create(
            channel_type='in_app', is_enabled=True, email_recipients=[]
        )

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(self.plain)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_allowed_and_serializer_whitelist(self):
        self.client.force_authenticate(self.staff)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results'] if isinstance(resp.data, dict) and 'results' in resp.data else resp.data
        item = results[0]
        # 序列化器白名单：仅契约字段，无意外暴露
        self.assertEqual(
            set(item.keys()),
            {'id', 'channel_type', 'channel_type_display', 'is_enabled',
             'email_recipients', 'email_subject_prefix',
             'min_severity', 'min_severity_display', 'alert_types',
             'created_at', 'updated_at'},
        )

    def test_unauthenticated_forbidden(self):
        resp = self.client.get(self.list_url)
        self.assertIn(resp.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED))
