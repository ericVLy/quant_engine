from django.db import models
from django.conf import settings
from apps.suites.models import Suite

class Plan(models.Model):
    TRIGGER_CHOICES = [
        ('time', '时间驱动'),
        ('event', '事件驱动'),
        ('manual', '手动触发'),
    ]
    EXEC_MODE_CHOICES = [
        ('serial', '串行'),
        ('parallel', '并行'),
        ('fail_stop', '失败停止'),
    ]
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('archived', '已归档'),
    ]
    name = models.CharField(max_length=100)
    root_suite = models.ForeignKey(Suite, on_delete=models.PROTECT, related_name='plans')
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default='time')
    cron_expr = models.CharField(max_length=100, blank=True, null=True)
    event_type = models.CharField(max_length=50, blank=True, null=True)
    symbol_scope = models.JSONField(default=dict)
    exec_mode = models.CharField(max_length=20, choices=EXEC_MODE_CHOICES, default='serial')
    retry_policy = models.JSONField(default=dict, blank=True)
    # 资金占用：Plan 实例是账户资金的唯一占用者，向下按 Suite/Case 分级申请
    account_id = models.CharField(max_length=64, blank=True, verbose_name='交易账户ID')
    allocated_capital = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True,
        verbose_name='占用资金总额',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return self.name


class PlanVersion(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name='versions')
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-version',)
        constraints = [
            models.UniqueConstraint(fields=('plan', 'version'), name='unique_plan_version'),
        ]
