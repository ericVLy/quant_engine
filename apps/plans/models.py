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
    RUN_STATUS_CHOICES = [
        ('new', '未运行'),
        ('running', '运行中'),
        ('done', '已完成'),
        ('interrupt', '已中断'),
    ]
    SUITE_START_CHOICES = [
        ('auto', 'Plan 启动时自动启动 Suite'),
        ('manual', '手动启动 Suite'),
    ]
    #: 持仓方向限制（与 ``runner.risk.PositionPolicy.mode`` 同契约）
    POSITION_MODE_CHOICES = [
        ('both', '双向'),
        ('long_only', '仅做多'),
        ('short_only', '仅做空'),
        ('flat', '不开仓'),
    ]
    name = models.CharField(max_length=100)
    root_suite = models.ForeignKey(Suite, on_delete=models.PROTECT, related_name='plans')
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default='time')
    cron_expr = models.CharField(max_length=100, blank=True, null=True)
    event_type = models.CharField(max_length=50, blank=True, null=True)
    # 标的范围不再由 Plan 声明：改由 Case.params['symbol_scope'] 持有，
    # Plan 的实际标的集合 = 编排树内所有已发布 Case 声明范围的并集
    # （见 apps.plans.services.resolve_plan_symbols）。
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
    # 运行状态：创建即 new；树内 suite 全部完成 → done；失败/手动停止 → interrupt
    run_status = models.CharField(max_length=10, choices=RUN_STATUS_CHOICES, default='new')
    # Suite 启动模式：Plan 启动时自动启动根 Suite，或手动启动
    suite_start_mode = models.CharField(max_length=10, choices=SUITE_START_CHOICES, default='manual')

    # ---- Plan 级风控限额（F1）----
    # 限额随策略配置存在 DB 里，调度器/执行端每次执行时读取（随 PlanRegistry 热加载），
    # 因此**改限额不需要重启进程**。全部可空：``None`` 表示"不设限"（而不是 0）。
    # 落地背景：``RiskController`` 早已实现这些策略，但此前生产装配
    # （``build_execution_service`` / ``run_scheduler``）没有任何入口能配置它们，
    # 实际只有"交易时段"和"volume > 0"在生效。
    risk_position_mode = models.CharField(
        max_length=10, choices=POSITION_MODE_CHOICES, default='both',
        verbose_name='持仓方向限制',
        help_text='both=双向 / long_only=仅做多 / short_only=仅做空 / flat=不开仓',
    )
    risk_max_order_volume = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='单笔数量上限',
    )
    risk_max_order_value = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True, verbose_name='单笔金额上限',
    )
    risk_max_daily_value = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True, verbose_name='每日累计金额上限',
        help_text='按当日已挂用的委托单金额（price × volume）累计；留空不限制',
    )
    risk_max_account_value = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True,
        verbose_name='账户可用资金上限',
        help_text='下单前校验账户可用资金是否覆盖本笔金额；需接账户快照来源',
    )
    risk_max_position_value = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True, verbose_name='总仓位金额上限',
    )
    risk_max_position_volume = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='总仓位数量上限',
    )
    risk_allowed_sessions = models.JSONField(
        null=True, blank=True, verbose_name='交易时段窗口',
        help_text='形如 [[9,30,11,30],[13,0,15,0]] 或 [[[9,30],[11,30]]]；留空用默认 A 股时段',
    )
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
