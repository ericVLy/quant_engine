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
    name = models.CharField(max_length=100)
    node_type = models.CharField(max_length=20, choices=NODE_TYPE_CHOICES)
    params = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.name} (v{self.version})"
