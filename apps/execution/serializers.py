from rest_framework import serializers
from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order, FundAllocation
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