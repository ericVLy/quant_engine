from rest_framework import serializers

from .models import Case


class CaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Case
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'version', 'status')

    def validate_params(self, value):
        from apps.execution.registry import EventRegistry

        if not isinstance(value, dict):
            raise serializers.ValidationError('params 必须是 JSON 对象')

        trigger = value.get('trigger')
        if trigger is None:
            return value
        if not isinstance(trigger, dict) or not trigger.get('event_type'):
            raise serializers.ValidationError('trigger 必须包含 event_type')
        if not EventRegistry.validate(trigger['event_type']):
            raise serializers.ValidationError(
                f"未注册的事件类型: {trigger['event_type']}"
            )
        return value
