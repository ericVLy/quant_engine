from django.contrib import admin
from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order, Alert, AlertChannel, AccountFundConfig, FundAllocation


@admin.register(EventTypeRegistry)
class EventTypeRegistryAdmin(admin.ModelAdmin):
    list_display = ('name', 'scope', 'plugin_id', 'is_active', 'created_at')
    search_fields = ('name', 'description')
    list_filter = ('scope', 'is_active')


@admin.register(SuiteRun)
class SuiteRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'plan', 'suite', 'symbol', 'status', 'started_at', 'ended_at')
    list_filter = ('status',)
    search_fields = ('symbol',)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('id', 'run', 'event_type', 'source', 'status', 'created_at')
    list_filter = ('event_type', 'status')
    search_fields = ('source',)


@admin.register(ExecutionLog)
class ExecutionLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'symbol', 'plan', 'final_direction', 'status', 'trigger_time')
    list_filter = ('status', 'final_direction')
    search_fields = ('symbol',)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'symbol', 'direction', 'price', 'volume', 'status', 'created_at')
    list_filter = ('status', 'direction')
    search_fields = ('symbol',)


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'alert_type', 'severity', 'status', 'in_app_notified', 'email_notified', 'created_at')
    list_filter = ('alert_type', 'severity', 'status', 'in_app_notified', 'email_notified')
    search_fields = ('title', 'message', 'error_code')
    readonly_fields = ('in_app_notified', 'email_notified', 'notification_error', 'created_at', 'updated_at')
    
    fieldsets = (
        ('基本信息', {
            'fields': ('alert_type', 'severity', 'status', 'title', 'message', 'error_code')
        }),
        ('关联实体', {
            'fields': ('plan', 'suite_run', 'order')
        }),
        ('通知状态', {
            'fields': ('in_app_notified', 'email_notified', 'notification_error')
        }),
        ('处理信息', {
            'fields': ('acknowledged_by', 'acknowledged_at', 'resolved_by', 'resolved_at')
        }),
        ('时间信息', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    actions = ['acknowledge_alerts', 'resolve_alerts']
    
    def acknowledge_alerts(self, request, queryset):
        """批量确认告警"""
        from django.utils import timezone
        updated = queryset.filter(status='pending').update(
            status='acknowledged',
            acknowledged_by=request.user,
            acknowledged_at=timezone.now()
        )
        self.message_user(request, f'已确认 {updated} 个告警')
    acknowledge_alerts.short_description = '确认选中的告警'
    
    def resolve_alerts(self, request, queryset):
        """批量解决告警"""
        from django.utils import timezone
        updated = queryset.filter(status__in=['pending', 'acknowledged']).update(
            status='resolved',
            resolved_by=request.user,
            resolved_at=timezone.now()
        )
        self.message_user(request, f'已解决 {updated} 个告警')
    resolve_alerts.short_description = '解决选中的告警'


@admin.register(AlertChannel)
class AlertChannelAdmin(admin.ModelAdmin):
    list_display = ('id', 'channel_type', 'is_enabled', 'min_severity', 'created_at')
    list_filter = ('channel_type', 'is_enabled', 'min_severity')
    
    fieldsets = (
        ('基本配置', {
            'fields': ('channel_type', 'is_enabled')
        }),
        ('过滤配置', {
            'fields': ('min_severity', 'alert_types')
        }),
        ('邮件配置', {
            'fields': ('email_recipients', 'email_subject_prefix'),
            'description': '仅当渠道类型为"邮件通知"时使用'
        }),
    )
    
    actions = ['enable_channels', 'disable_channels']
    
    def enable_channels(self, request, queryset):
        """批量启用渠道"""
        updated = queryset.update(is_enabled=True)
        # 重新加载渠道配置
        from .alerts import alert_service
        alert_service.reload_channels()
        self.message_user(request, f'已启用 {updated} 个告警渠道')
    enable_channels.short_description = '启用选中的渠道'
    
    def disable_channels(self, request, queryset):
        """批量禁用渠道"""
        updated = queryset.update(is_enabled=False)
        # 重新加载渠道配置
        from .alerts import alert_service
        alert_service.reload_channels()
        self.message_user(request, f'已禁用 {updated} 个告警渠道')
    disable_channels.short_description = '禁用选中的渠道'


@admin.register(AccountFundConfig)
class AccountFundConfigAdmin(admin.ModelAdmin):
    list_display = ('account_id', 'total_capital', 'allocated_capital', 'available_capital')
    search_fields = ('account_id',)


@admin.register(FundAllocation)
class FundAllocationAdmin(admin.ModelAdmin):
    list_display = ('id', 'level', 'plan', 'suite', 'case', 'amount', 'used_amount', 'status', 'created_at')
    list_filter = ('level', 'status')
    search_fields = ('plan__name', 'suite__name', 'case__name')