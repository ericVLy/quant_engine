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


class NodeRun(models.Model):
    """编排树中每个节点（Suite/Case）的运行实例，支持父子层级与执行轨迹回放。"""
    NODE_TYPE_CHOICES = [('suite', 'Suite 节点'), ('case', 'Case 节点')]
    STATUS_CHOICES = [
        ('pending', '待执行'),
        ('running', '执行中'),
        ('completed', '已完成'),
        ('failed', '失败'),
        ('skipped', '已跳过'),
    ]
    run = models.ForeignKey(SuiteRun, on_delete=models.CASCADE, related_name='node_runs')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    node_type = models.CharField(max_length=10, choices=NODE_TYPE_CHOICES)
    suite = models.ForeignKey(Suite, on_delete=models.SET_NULL, null=True, blank=True, related_name='node_runs')
    case = models.ForeignKey('cases.Case', on_delete=models.SET_NULL, null=True, blank=True, related_name='node_runs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    direction = models.SmallIntegerField(default=0)
    result = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        target = self.suite_id if self.node_type == 'suite' else self.case_id
        return f"{self.node_type}:{target} - {self.status}"


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
    task_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    error_code = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=[('success','成功'),('failed','失败'),('blocked','风控拦截')], default='success')

    class Meta:
        indexes = [models.Index(fields=['symbol', '-trigger_time']), models.Index(fields=['plan', '-trigger_time'])]

    def __str__(self):
        return f"{self.symbol} @ {self.trigger_time}"


class AccountFundConfig(models.Model):
    """交易账户资金配置（单账户单行）：账户总资金是 Plan 占用资金的上限。"""
    account_id = models.CharField(max_length=64, blank=True, verbose_name='交易账户ID', unique=True)
    total_capital = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='账户总资金')

    class Meta:
        verbose_name = '账户资金配置'
        verbose_name_plural = '账户资金配置'

    def __str__(self):
        return f'{self.account_id or "default"} total={self.total_capital}'

    @property
    def allocated_capital(self):
        """该账户下所有 Plan 占用资金之和。"""
        from django.db.models import Sum
        total = Plan.objects.filter(account_id=self.account_id).aggregate(
            total=Sum('allocated_capital'),
        )['total']
        return total or 0

    @property
    def available_capital(self):
        """空闲资金 = 总资金 - 已占用。"""
        return self.total_capital - self.allocated_capital


class FundAllocation(models.Model):
    """分级资金申请：Plan 占用账户资金，Suite 向 Plan 申请，Case 向 Suite 申请。

    - plan 级：suite/case 均为空，额度上限为 Plan.allocated_capital；
    - suite 级：挂在 (plan, suite)，同一 Plan 下所有 suite 级申请之和不得超过 plan 级额度；
    - case 级：挂在 (plan, suite, case)，同一 suite 下所有 case 级申请之和不得超过该 suite 级额度。
    运行时下单按 case → suite → plan 就近扣减 ``used_amount``（行级锁保证并发安全）。
    """
    LEVEL_CHOICES = [('plan', 'Plan 级'), ('suite', 'Suite 级'), ('case', 'Case 级')]
    STATUS_CHOICES = [('active', '生效中'), ('released', '已释放')]

    level = models.CharField(max_length=10, choices=LEVEL_CHOICES)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name='fund_allocations')
    suite = models.ForeignKey(Suite, on_delete=models.CASCADE, null=True, blank=True, related_name='fund_allocations')
    case = models.ForeignKey('cases.Case', on_delete=models.CASCADE, null=True, blank=True, related_name='fund_allocations')
    amount = models.DecimalField(max_digits=16, decimal_places=2, verbose_name='申请额度')
    used_amount = models.DecimalField(max_digits=16, decimal_places=2, default=0, verbose_name='已占用金额')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['plan'], condition=models.Q(level='plan'),
                name='unique_plan_level_allocation',
            ),
            models.UniqueConstraint(
                fields=['plan', 'suite'],
                condition=models.Q(level='suite'),
                name='unique_suite_level_allocation',
            ),
            models.UniqueConstraint(
                fields=['plan', 'suite', 'case'],
                condition=models.Q(level='case'),
                name='unique_case_level_allocation',
            ),
        ]

    def __str__(self):
        target = self.case_id or self.suite_id or self.plan_id
        return f'{self.level}:{target} amount={self.amount} used={self.used_amount}'


class Order(models.Model):
    DIRECTION_CHOICES = [('buy','买入'),('sell','卖出')]
    STATUS_CHOICES = [
        ('pending', '待发送'), ('sent', '已发送'), ('filled', '已成交'),
        ('rejected', '已拒绝'), ('canceled', '已撤单'),
    ]
    log = models.ForeignKey(ExecutionLog, on_delete=models.CASCADE, related_name='orders')
    symbol = models.CharField(max_length=20)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    price = models.DecimalField(max_digits=12, decimal_places=4)
    volume = models.PositiveIntegerField()
    filled_volume = models.PositiveIntegerField(default=0)
    external_order_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    last_error = models.TextField(blank=True)
    report_payload = models.JSONField(default=dict, blank=True)
    # 下单时从哪个资金额度扣减（FundAllocation），用于失败回退与审计
    fund_allocation = models.ForeignKey(
        'FundAllocation', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders',
    )
    # 已处理的回报指纹（外部 order id + 状态 + 累计成交量 + 价格），用于重复回报幂等去重
    processed_report_keys = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.symbol} {self.direction} {self.volume}@{self.price}"


class Alert(models.Model):
    """告警模型：记录系统中的告警信息"""
    ALERT_TYPE_CHOICES = [
        ('order_failed', '订单失败'),
        ('suite_failed', '策略执行失败'),
        ('plan_failed', '计划执行失败'),
        ('risk_violation', '风控违规'),
        ('system_error', '系统错误'),
    ]
    SEVERITY_CHOICES = [
        ('low', '低'),
        ('medium', '中'),
        ('high', '高'),
        ('critical', '紧急'),
    ]
    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('acknowledged', '已确认'),
        ('resolved', '已解决'),
    ]
    
    alert_type = models.CharField(max_length=50, choices=ALERT_TYPE_CHOICES, verbose_name="告警类型")
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='medium', verbose_name="严重程度")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="状态")
    
    # 关联的实体信息
    plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name='alerts')
    suite_run = models.ForeignKey(SuiteRun, on_delete=models.SET_NULL, null=True, blank=True, related_name='alerts')
    # 注意：这里使用字符串引用，避免循环导入
    order = models.ForeignKey('Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='alerts')
    
    # 告警详情
    title = models.CharField(max_length=200, verbose_name="标题")
    message = models.TextField(verbose_name="详细消息")
    error_code = models.CharField(max_length=50, blank=True, null=True, verbose_name="错误代码")
    
    # 通知状态
    in_app_notified = models.BooleanField(default=False, verbose_name="应用内已通知")
    email_notified = models.BooleanField(default=False, verbose_name="邮件已通知")
    notification_error = models.TextField(blank=True, verbose_name="通知错误信息")
    
    # 处理信息
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='acknowledged_alerts'
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='resolved_alerts'
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '告警'
        verbose_name_plural = '告警'
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['status', 'severity']),
            models.Index(fields=['alert_type', '-created_at']),
        ]

    def __str__(self):
        return f"[{self.severity.upper()}] {self.title}"


class AlertChannel(models.Model):
    """告警渠道配置：支持应用内通知和邮件通知"""
    CHANNEL_TYPE_CHOICES = [
        ('in_app', '应用内通知'),
        ('email', '邮件通知'),
    ]
    
    channel_type = models.CharField(max_length=20, choices=CHANNEL_TYPE_CHOICES, unique=True, verbose_name="渠道类型")
    is_enabled = models.BooleanField(default=True, verbose_name="是否启用")
    
    # 邮件相关配置（仅当 channel_type='email' 时使用）
    email_recipients = models.JSONField(default=list, blank=True, verbose_name="邮件收件人列表")
    email_subject_prefix = models.CharField(max_length=50, default="[量化交易系统]", verbose_name="邮件主题前缀")
    
    # 过滤配置
    min_severity = models.CharField(
        max_length=20, choices=Alert.SEVERITY_CHOICES, default='low',
        verbose_name="最低告警级别"
    )
    alert_types = models.JSONField(default=list, blank=True, verbose_name="告警类型白名单（空表示全部）")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '告警渠道配置'
        verbose_name_plural = '告警渠道配置'

    def __str__(self):
        return f"{self.get_channel_type_display()} ({'启用' if self.is_enabled else '禁用'})"
    
    def should_send_alert(self, alert):
        """判断是否应该发送该告警"""
        if not self.is_enabled:
            return False
        
        # 检查严重程度
        severity_order = {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}
        if severity_order.get(alert.severity, 0) < severity_order.get(self.min_severity, 0):
            return False
        
        # 检查告警类型白名单
        if self.alert_types and alert.alert_type not in self.alert_types:
            return False
        
        return True