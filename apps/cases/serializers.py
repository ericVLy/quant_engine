import math

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

    def is_number(item):
        if isinstance(item, bool):
            return False
        if isinstance(item, (int, float)):
            return math.isfinite(item)
        if isinstance(item, str):
            try:
                return math.isfinite(float(item.strip()))
            except (TypeError, ValueError):
                return False
        return False

    def validate_number(path, item):
        if not is_number(item):
            invalid(path, '必须是有限数值')

    def validate_positive_number(path, item):
        validate_number(path, item)
        if float(item) <= 0:
            invalid(path, '必须大于 0')

    def validate_integer(path, item, minimum=None):
        if isinstance(item, bool) or not isinstance(item, int):
            invalid(path, '必须是整数')
        if minimum is not None and item < minimum:
            invalid(path, f'必须大于等于 {minimum}')

    def validate_string(path, item):
        if not isinstance(item, str) or not item.strip():
            invalid(path, '必须是非空字符串')

    def validate_order(order, path='order'):
        if not isinstance(order, dict):
            invalid(path, '必须是对象')
        allowed = {'direction', 'price', 'volume'}
        unknown_order = set(order) - allowed
        if unknown_order:
            invalid(path, f'不允许的字段: {", ".join(sorted(unknown_order))}')
        missing = allowed - set(order)
        if missing:
            invalid(path, f'缺少字段: {", ".join(sorted(missing))}')
        if order['direction'] not in ('buy', 'sell'):
            invalid(f'{path}.direction', '必须是 buy 或 sell')
        validate_positive_number(f'{path}.price', order['price'])
        validate_integer(f'{path}.volume', order['volume'], minimum=1)

    def validate_result(result):
        if not isinstance(result, dict):
            invalid('result', '必须是对象')
        allowed_result = {'direction', 'payload', 'order'}
        unknown_result = set(result) - allowed_result
        if unknown_result:
            invalid('result', f'不允许的字段: {", ".join(sorted(unknown_result))}')
        if 'direction' in result:
            validate_integer('result.direction', result['direction'])
            if result['direction'] not in (-1, 0, 1):
                invalid('result.direction', '必须是 -1、0 或 1')
        if 'payload' in result and not isinstance(result['payload'], dict):
            invalid('result.payload', '必须是对象')
        if 'order' in result:
            validate_order(result['order'], 'result.order')

    def validate_filter(filter_cfg):
        if not isinstance(filter_cfg, dict):
            invalid('filter', '必须是对象')
        allowed_filter = {'op', 'field', 'threshold', 'value'}
        unknown_filter = set(filter_cfg) - allowed_filter
        if unknown_filter:
            invalid('filter', f'不允许的字段: {", ".join(sorted(unknown_filter))}')
        if 'op' in filter_cfg and filter_cfg['op'] not in ('keep', 'drop'):
            invalid('filter.op', '必须是 keep 或 drop')
        if 'field' in filter_cfg:
            validate_string('filter.field', filter_cfg['field'])
        for key in ('threshold', 'value'):
            if key in filter_cfg:
                validate_number(f'filter.{key}', filter_cfg[key])
        if 'field' in filter_cfg and not ({'threshold', 'value'} & set(filter_cfg)):
            invalid('filter', '提供 field 时必须同时提供 threshold 或 value')

    def validate_verdict(verdict_cfg):
        if not isinstance(verdict_cfg, dict):
            invalid('verdict', '必须是对象')
        allowed_verdict = {'method', 'components'}
        unknown_verdict = set(verdict_cfg) - allowed_verdict
        if unknown_verdict:
            invalid('verdict', f'不允许的字段: {", ".join(sorted(unknown_verdict))}')
        if 'method' in verdict_cfg and verdict_cfg['method'] not in ('weighted_sum', 'vote'):
            invalid('verdict.method', '必须是 weighted_sum 或 vote')
        components = verdict_cfg.get('components')
        if not isinstance(components, list) or not components:
            invalid('verdict.components', '必须是非空数组')
        component_keys = {
            'indicator', 'calculation', 'field', 'high_field', 'low_field',
            'period', 'fast', 'slow', 'signal', 'threshold',
            'threshold_oversold', 'threshold_overbought', 'weight',
        }
        for index, component in enumerate(components):
            path = f'verdict.components[{index}]'
            if not isinstance(component, dict):
                invalid(path, '必须是对象')
            unknown_component = set(component) - component_keys
            if unknown_component:
                invalid(path, f'不允许的字段: {", ".join(sorted(unknown_component))}')
            if not (component.get('indicator') or component.get('calculation')):
                invalid(path, '必须提供 indicator 或 calculation')
            for key in ('field', 'high_field', 'low_field'):
                if key in component:
                    validate_string(f'{path}.{key}', component[key])
            for key in ('period', 'fast', 'slow', 'signal'):
                if key in component:
                    validate_integer(f'{path}.{key}', component[key], minimum=1)
            for key in ('threshold', 'threshold_oversold', 'threshold_overbought', 'weight'):
                if key in component:
                    validate_number(f'{path}.{key}', component[key])
            if 'weight' in component and float(component['weight']) < 0:
                invalid(f'{path}.weight', '不能小于 0')

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
    if 'period' in value:
        validate_integer('period', value['period'], minimum=1)
    if 'direction' in value and (isinstance(value['direction'], bool) or value['direction'] not in (-1, 0, 1)):
        invalid('direction', '必须是 -1、0 或 1')
    for key in ('threshold_oversold', 'threshold_overbought', 'threshold', 'weight'):
        if key in value:
            validate_number(key, value[key])
    for key in ('calculation', 'indicator', 'field', 'high_field', 'low_field', 'node_type'):
        if key in value:
            validate_string(key, value[key])
    for key in ('fast', 'slow', 'signal'):
        if key in value:
            validate_integer(key, value[key], minimum=1)
    if 'result' in value:
        validate_result(value['result'])
    order = value.get('order')
    if order is not None:
        if not isinstance(order, dict):
            invalid('order', '必须是对象')
        validate_order(order)
    if 'filter' in value:
        validate_filter(value['filter'])
    if 'verdict' in value:
        validate_verdict(value['verdict'])

    if node_type == 'executor' and 'order' in value and 'result' in value:
        if 'order' in value['result'] and value['order'] != value['result']['order']:
            invalid('order', '与 result.order 不一致')


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
