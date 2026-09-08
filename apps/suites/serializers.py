from rest_framework import serializers

from apps.cases.models import Case

from .models import Edge, Suite


def validate_event_condition(value):
    if not isinstance(value, dict):
        raise serializers.ValidationError('event_condition 必须是 JSON 对象')

    allowed_keys = {'event_type', 'case_id', 'next_event', 'op', 'field', 'threshold'}
    unknown = set(value.keys()) - allowed_keys
    if unknown:
        raise serializers.ValidationError(f'event_condition 不允许的字段: {", ".join(sorted(unknown))}')

    if 'event_type' not in value or not value['event_type']:
        raise serializers.ValidationError('event_condition.event_type 是必填字段')

    if 'case_id' in value and (not isinstance(value['case_id'], int) or isinstance(value['case_id'], bool)):
        raise serializers.ValidationError('event_condition.case_id 必须是整数')

    if 'next_event' in value and (not isinstance(value['next_event'], str) or not value['next_event']):
        raise serializers.ValidationError('event_condition.next_event 必须是非空字符串')

    _validate_operator(value)
    return value


def _validate_operator(value):
    """校验操作符契约：op + field + threshold 必须成组，op 属于允许集合。

    允许的 op：eq / neq / gt / gte / lt / lte / between
    """
    allowed_ops = {'eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between'}
    present = {k for k in ('op', 'field', 'threshold') if k in value}
    if not present:
        if 'field' in value or 'threshold' in value:
            raise serializers.ValidationError('提供 field/threshold 时必须同时提供 op')
        return value

    # 三者必须同时出现
    if present != {'op', 'field', 'threshold'}:
        raise serializers.ValidationError('op / field / threshold 必须同时提供')
    op = value['op']
    if op not in allowed_ops:
        raise serializers.ValidationError(f'不允许的操作符: {op}（允许 {sorted(allowed_ops)}）')
    if not value['field'] or not isinstance(value['field'], str):
        raise serializers.ValidationError('event_condition.field 必须是非空字符串')
    threshold = value['threshold']
    if op == 'between':
        if not (isinstance(threshold, (list, tuple)) and len(threshold) == 2):
            raise serializers.ValidationError('between 操作符的 threshold 必须是双元素数组 [低, 高]')
        lo, hi = threshold
        if not isinstance(lo, (int, float)) or isinstance(lo, bool) or \
           not isinstance(hi, (int, float)) or isinstance(hi, bool):
            raise serializers.ValidationError('between 的边界必须是数值')
        if lo > hi:
            raise serializers.ValidationError('between 的低边界不能大于高边界')
    elif not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise serializers.ValidationError('threshold 必须是数值')
    return value


class EdgeSerializer(serializers.ModelSerializer):
    event_condition = serializers.JSONField(validators=[validate_event_condition])

    class Meta:
        model = Edge
        fields = '__all__'


class SuiteSerializer(serializers.ModelSerializer):
    case_ids = serializers.PrimaryKeyRelatedField(
        source='cases',
        many=True,
        queryset=Case.objects.all(),
        required=False,
        write_only=True,
    )
    cases = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Suite
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'version', 'status', 'run_status')

