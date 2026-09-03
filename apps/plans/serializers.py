from rest_framework import serializers

from apps.execution.registry import EventRegistry

from .models import Plan


def validate_cron_expression(value):
    if not isinstance(value, str) or len(value.split()) != 5:
        raise serializers.ValidationError('cron_expr 必须包含 5 个字段')
    allowed = set('0123456789*/?,ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-')
    if any(not field or set(field) - allowed for field in value.split()):
        raise serializers.ValidationError('cron_expr 包含非法字符')
    return value


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'version', 'status')

    def validate_symbol_scope(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('symbol_scope 必须是 JSON 对象')

        allowed_types = {'all', 'groups', 'symbols'}
        allowed_keys = {'type', 'group_ids', 'symbol_codes'}
        scope_type = value.get('type')

        if scope_type not in allowed_types:
            raise serializers.ValidationError('symbol_scope.type 必须是 all、groups 或 symbols')

        unknown = set(value.keys()) - allowed_keys
        if unknown:
            raise serializers.ValidationError(f'symbol_scope 不允许的字段: {", ".join(sorted(unknown))}')

        if scope_type == 'all':
            if value.keys() - {'type'}:
                raise serializers.ValidationError('all 类型只能包含 type 字段')
        if scope_type == 'groups':
            if not isinstance(value.get('group_ids'), list):
                raise serializers.ValidationError('groups 类型必须提供 group_ids 数组')
            if set(value.keys()) - {'type', 'group_ids'}:
                raise serializers.ValidationError('groups 类型只允许 type 和 group_ids 字段')
        if scope_type == 'symbols':
            if not isinstance(value.get('symbol_codes'), list):
                raise serializers.ValidationError('symbols 类型必须提供 symbol_codes 数组')
            if set(value.keys()) - {'type', 'symbol_codes'}:
                raise serializers.ValidationError('symbols 类型只允许 type 和 symbol_codes 字段')
        return value

    def validate(self, attrs):
        trigger_type = attrs.get('trigger_type', getattr(self.instance, 'trigger_type', 'time'))
        cron_expr = attrs.get('cron_expr', getattr(self.instance, 'cron_expr', None))
        event_type = attrs.get('event_type', getattr(self.instance, 'event_type', None))

        if trigger_type == 'time':
            if not cron_expr:
                raise serializers.ValidationError({'cron_expr': '时间触发的 Plan 必须提供 cron_expr'})
            try:
                validate_cron_expression(cron_expr)
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({'cron_expr': exc.detail}) from exc
        elif trigger_type == 'event':
            if not event_type:
                raise serializers.ValidationError({'event_type': '事件触发的 Plan 必须提供 event_type'})
            if not EventRegistry.validate(event_type):
                raise serializers.ValidationError({'event_type': f'未注册的事件类型: {event_type}'})
        return attrs
