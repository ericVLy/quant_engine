from rest_framework import serializers
from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order
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


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = '__all__'