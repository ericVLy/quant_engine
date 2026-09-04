from rest_framework import serializers

from .models import Case, CaseVersion


CASE_SCHEMA = {
    'type': 'object',
    'properties': {
        'trigger': {
            'type': 'object',
            'required': ['event_type'],
            'properties': {'event_type': {'type': 'string', 'minLength': 1}},
            'additionalProperties': True,
        },
        'period': {'type': 'integer', 'minimum': 1},
        'threshold_oversold': {'type': 'number'},
        'threshold_overbought': {'type': 'number'},
        'direction': {'type': 'integer', 'enum': [-1, 0, 1]},
        'result': {'type': 'object'},
        'order': {
            'type': 'object',
            'required': ['direction', 'price', 'volume'],
            'properties': {
                'direction': {'type': 'string', 'enum': ['buy', 'sell']},
                'price': {'type': ['number', 'string']},
                'volume': {'type': 'integer', 'minimum': 1},
            },
            'additionalProperties': True,
        },
        'calculation': {'type': 'string'},
        'indicator': {'type': 'string'},
        'field': {'type': 'string'},
        'high_field': {'type': 'string'},
        'low_field': {'type': 'string'},
        'fast': {'type': 'integer', 'minimum': 1},
        'slow': {'type': 'integer', 'minimum': 1},
        'signal': {'type': 'integer', 'minimum': 1},
        'threshold': {'type': 'number'},
        'weight': {'type': 'number'},
        'node_type': {'type': 'string'},
        'filter': {
            'type': 'object',
            'properties': {
                'op': {'type': 'string', 'enum': ['keep', 'drop']},
                'field': {'type': 'string'},
                'threshold': {'type': 'number'},
                'value': {'type': 'number'},
            },
            'additionalProperties': True,
        },
        'verdict': {
            'type': 'object',
            'properties': {
                'method': {'type': 'string', 'enum': ['weighted_sum', 'vote']},
                'components': {'type': 'array'},
            },
            'additionalProperties': True,
        },
    },
    'additionalProperties': True,
}


def validate_case_schema(node_type, value):
    def invalid(path, message):
        raise serializers.ValidationError(f'{node_type} 参数 {path}: {message}')

    allowed_keys = {
        'trigger', 'period', 'threshold_oversold', 'threshold_overbought',
        'direction', 'result', 'order',
        # 真实因子计算字段（signal/filter/verdict）
        'calculation', 'indicator', 'field', 'high_field', 'low_field',
        'fast', 'slow', 'signal', 'threshold', 'weight', 'filter',
        'verdict', 'node_type',
    }
    if not isinstance(value, dict):
        invalid('params', '必须是对象')

    unknown = set(value.keys()) - allowed_keys
    if unknown:
        invalid('params', f'不允许的字段: {", ".join(sorted(unknown))}')

    if 'trigger' in value:
        trigger = value['trigger']
        if not isinstance(trigger, dict) or not isinstance(trigger.get('event_type'), str) or not trigger['event_type']:
            invalid('trigger', '必须是包含非空 event_type 的对象')
        if set(trigger.keys()) - {'event_type'}:
            invalid('trigger', '只允许 event_type 字段')
    if 'period' in value and (isinstance(value['period'], bool) or not isinstance(value['period'], int) or value['period'] < 1):
        invalid('period', '必须是大于等于 1 的整数')
    if 'direction' in value and value['direction'] not in (-1, 0, 1):
        invalid('direction', '必须是 -1、0 或 1')
    if 'result' in value and not isinstance(value['result'], dict):
        invalid('result', '必须是对象')
    order = value.get('order')
    if order is not None:
        if not isinstance(order, dict):
            invalid('order', '必须是对象')
        allowed_order_keys = {'direction', 'price', 'volume'}
        unknown_order = set(order.keys()) - allowed_order_keys
        if unknown_order:
            invalid('order', f'不允许的字段: {", ".join(sorted(unknown_order))}')
        missing = allowed_order_keys - order.keys()
        if missing:
            invalid('order', f'缺少字段: {", ".join(sorted(missing))}')
        if order.get('direction') not in ('buy', 'sell'):
            invalid('order.direction', '必须是 buy 或 sell')
        if isinstance(order.get('volume'), bool) or not isinstance(order.get('volume'), int) or order['volume'] < 1:
            invalid('order.volume', '必须是大于等于 1 的整数')


class CaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Case
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'version', 'status')

    def validate_params(self, value):
        from apps.execution.registry import EventRegistry

        if not isinstance(value, dict):
            raise serializers.ValidationError('params 必须是 JSON 对象')

        validate_case_schema(self.initial_data.get('node_type', getattr(self.instance, 'node_type', '')), value)

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


class CaseVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CaseVersion
        fields = ('id', 'case', 'version', 'name', 'node_type', 'params', 'status', 'created_at')
