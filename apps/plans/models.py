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
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return self.name
