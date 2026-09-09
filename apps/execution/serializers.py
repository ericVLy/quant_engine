from rest_framework import serializers
from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order, FundAllocation, Alert, AlertChannel
from .funds import FundError, allocate_funds
from .registry import EventRegistry


class EventTypeRegistrySerializer(serializers.ModelSerializer):
    is_active = serializers.BooleanField(default=True)

    class Meta:
        model = EventTypeRegistry
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def validate_name(self, value):
        """确保新事件类型不与内置事件冲突"""
        from .events import EventType
        if value in EventType.all():
            raise serializers.ValidationError(f"'{value}' 是系统内置事件，不允许重复注册")
        return value


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        fields = '__all__'
        read_only_fields = ('created_at',)

    def validate_event_type(self, value):
        """校验事件类型是否已注册"""
        if not EventRegistry.validate(value):
            raise serializers.ValidationError(f"未注册的事件类型: {value}")
        return value


class SuiteRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = SuiteRun
        fields = '__all__'
        read_only_fields = ('created_at',)


class ExecutionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionLog
        fields = '__all__'


class FundAllocationSerializer(serializers.ModelSerializer):
    """分级资金申请：Plan 占用 → Suite 申请 → Case 申请，层级校验由
    ``funds.allocate_funds`` 统一执行。"""

    class Meta:
        model = FundAllocation
        fields = '__all__'
        read_only_fields = ('used_amount', 'status', 'level', 'created_at', 'updated_at')

    def validate(self, attrs):
        plan = attrs.get('plan') or getattr(self.instance, 'plan', None)
        suite = attrs.get('suite') or getattr(self.instance, 'suite', None)
        case = attrs.get('case') or getattr(self.instance, 'case', None)
        amount = attrs.get('amount') or getattr(self.instance, 'amount', None)
        if plan is None:
            raise serializers.ValidationError('plan 必填')
        if amount is None:
            raise serializers.ValidationError('amount 必填')
        if case is not None and suite is None:
            raise serializers.ValidationError('case 级资金申请必须同时指定 suite')
        try:
            self._allocation = allocate_funds(plan, suite=suite, case=case, amount=amount)
        except FundError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return attrs

    def create(self, validated_data):
        return self._allocation

    def update(self, instance, validated_data):
        return self._allocation


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = '__all__'


class AlertSerializer(serializers.ModelSerializer):
    """告警序列化器"""
    alert_type_display = serializers.CharField(source='get_alert_type_display', read_only=True)
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True, allow_null=True)
    suite_run_display = serializers.CharField(source='suite_run.__str__', read_only=True, allow_null=True)
    
    class Meta:
        model = Alert
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'in_app_notified', 'email_notified', 'notification_error')


class AlertChannelSerializer(serializers.ModelSerializer):
    """告警渠道配置序列化器"""
    channel_type_display = serializers.CharField(source='get_channel_type_display', read_only=True)
    min_severity_display = serializers.CharField(source='get_min_severity_display', read_only=True)
    
    class Meta:
        model = AlertChannel
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')
    
    def validate_alert_types(self, value):
        """验证告警类型列表"""
        if not isinstance(value, list):
            raise serializers.ValidationError("alert_types 必须是列表")
        
        valid_types = [choice[0] for choice in Alert.ALERT_TYPE_CHOICES]
        for alert_type in value:
            if alert_type not in valid_types:
                raise serializers.ValidationError(f"无效的告警类型: {alert_type}")
        
        return value
    
    def validate_email_recipients(self, value):
        """验证邮件收件人列表"""
        if not isinstance(value, list):
            raise serializers.ValidationError("email_recipients 必须是列表")
        
        for email in value:
            if not isinstance(email, str) or '@' not in email:
                raise serializers.ValidationError(f"无效的邮件地址: {email}")
        
        return value


class AlertActionSerializer(serializers.Serializer):
    """告警操作序列化器"""
    action = serializers.ChoiceField(choices=['acknowledge', 'resolve'])
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)