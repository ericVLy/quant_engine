from django.db import models
from django.conf import settings

class Case(models.Model):
    NODE_TYPE_CHOICES = [
        ('signal', '信号节点'),
        ('filter', '过滤器'),
        ('verdict', '裁决节点'),
        ('executor', '执行器'),
    ]
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('archived', '已归档'),
    ]
    RUN_STATUS_CHOICES = [
        ('new', '未运行'),
        ('running', '运行中'),
        ('done', '已完成'),
        ('failed', '失败'),
    ]
    name = models.CharField(max_length=100)
    node_type = models.CharField(max_length=20, choices=NODE_TYPE_CHOICES)
    params = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    # 运行状态：依托 Suite 运行；由引擎在执行时流转，非 running 的 Suite 中不可运行
    run_status = models.CharField(max_length=10, choices=RUN_STATUS_CHOICES, default='new')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.name} (v{self.version})"


class CaseVersion(models.Model):
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='versions')
    version = models.PositiveIntegerField()
    name = models.CharField(max_length=100)
    node_type = models.CharField(max_length=20, choices=Case.NODE_TYPE_CHOICES)
    params = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=Case.STATUS_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('case', 'version'), name='unique_case_version'),
        ]
        ordering = ('-version',)
