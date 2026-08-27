from django.db import models
from django.conf import settings
from apps.plans.models import Plan
from apps.suites.models import Suite


class SuiteRun(models.Model):
    STATUS_CHOICES = [
        ('pending', '待启动'),
        ('running', '运行中'),
        ('completed', '已完成'),
        ('failed', '失败'),
        ('stopped', '已停止'),
    ]
    plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='runs'
    )
    suite = models.ForeignKey(
        Suite,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='runs'
    )
    symbol = models.CharField(max_length=20, verbose_name="标的代码")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    event_queue = models.JSONField(default=list)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.suite.name if self.suite else '?'} @ {self.symbol} - {self.status}"


class Event(models.Model):
    """
    事件模型，event_type 不再限制 choices，由注册中心校验合法性
    """
    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('processing', '处理中'),
        ('done', '已完成'),
        ('failed', '失败'),
    ]
    run = models.ForeignKey(SuiteRun, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=50, db_index=True)  # 移除 choices
    source = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.event_type} - {self.source} ({self.status})"


class EventTypeRegistry(models.Model):
    """用户/插件自定义事件类型注册表"""
    SCOPE_CHOICES = [
        ('system', '系统内置'),
        ('plugin', '插件定义'),
        ('user', '用户自定义'),
    ]
    name = models.CharField(max_length=50, unique=True, verbose_name="事件类型名")
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default='user')
    plugin_id = models.CharField(max_length=50, blank=True, null=True, verbose_name="来源插件")
    description = models.CharField(max_length=200, blank=True, verbose_name="描述")
    payload_schema = models.JSONField(default=dict, blank=True, verbose_name="载荷 JSON Schema")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_event_type_registry'
        verbose_name = '事件类型注册'
        verbose_name_plural = '事件类型注册'

    def __str__(self):
        return f"{self.name} ({self.get_scope_display()})"


class ExecutionLog(models.Model):
    DIRECTION_CHOICES = [
        (-1, '卖出/做空'),
        (0, '观望/平仓'),
        (1, '买入/做多'),
    ]
    plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, related_name='execution_logs')
    symbol = models.CharField(max_length=20)
    trigger_time = models.DateTimeField(auto_now_add=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    final_direction = models.SmallIntegerField(choices=DIRECTION_CHOICES)
    node_snapshots = models.JSONField(default=dict, blank=True)
    error_msg = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=[('success','成功'),('failed','失败'),('blocked','风控拦截')], default='success')

    class Meta:
        indexes = [models.Index(fields=['symbol', '-trigger_time']), models.Index(fields=['plan', '-trigger_time'])]

    def __str__(self):
        return f"{self.symbol} @ {self.trigger_time}"


class Order(models.Model):
    DIRECTION_CHOICES = [('buy','买入'),('sell','卖出')]
    STATUS_CHOICES = [('pending','待发送'),('sent','已发送'),('filled','已成交'),('rejected','已拒绝')]
    log = models.ForeignKey(ExecutionLog, on_delete=models.CASCADE, related_name='orders')
    symbol = models.CharField(max_length=20)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    price = models.DecimalField(max_digits=12, decimal_places=4)
    volume = models.PositiveIntegerField()
    external_order_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.symbol} {self.direction} {self.volume}@{self.price}"