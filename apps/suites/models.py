from django.db import models
from django.conf import settings
from apps.cases.models import Case

class Suite(models.Model):
    AGGREGATE_CHOICES = [
        ('weighted_sum', '加权求和'),
        ('vote', '投票'),
        ('and', '逻辑与'),
        ('or', '逻辑或'),
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
        ('interrupt', '已中断'),
    ]
    name = models.CharField(max_length=100)
    aggregate_method = models.CharField(max_length=20, choices=AGGREGATE_CHOICES, default='weighted_sum')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    version = models.PositiveIntegerField(default=1)
    # Suite 占用资金：加入 Plan（作为根 Suite 或其子树成员）时受 Plan 空闲资金约束
    allocated_capital = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True,
        verbose_name='占用资金',
    )
    # 运行状态：cases 全部完成 → done；case 失败/手动停止 → interrupt
    run_status = models.CharField(max_length=10, choices=RUN_STATUS_CHOICES, default='new')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    cases = models.ManyToManyField(Case, related_name='suites', blank=True)

    def __str__(self):
        return self.name
class SuiteVersion(models.Model):
    """发布时的不可变拓扑快照，运行时引擎只读快照以保证执行一致性。"""
    suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name='versions')
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-version',)
        constraints = [
            models.UniqueConstraint(fields=('suite', 'version'), name='unique_suite_version'),
        ]

    def __str__(self):
        return f"{self.suite.name} v{self.version}"




class Edge(models.Model):
    from_suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name='out_edges')
    to_suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name='in_edges')
    condition = models.JSONField(default=dict, blank=True)
    event_condition = models.JSONField(default=dict, blank=True)
    weight = models.FloatField(default=1.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['from_suite', 'to_suite', 'condition']]

    def __str__(self):
        return f"{self.from_suite} -> {self.to_suite}"
