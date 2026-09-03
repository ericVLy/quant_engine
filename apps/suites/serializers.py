from rest_framework import serializers

from apps.cases.models import Case

from .models import Edge, Suite


def validate_event_condition(value):
    if not isinstance(value, dict):
        raise serializers.ValidationError('event_condition 必须是 JSON 对象')

    allowed_keys = {'event_type', 'case_id', 'next_event'}
    unknown = set(value.keys()) - allowed_keys
    if unknown:
        raise serializers.ValidationError(f'event_condition 不允许的字段: {", ".join(sorted(unknown))}')

    if 'event_type' not in value or not value['event_type']:
        raise serializers.ValidationError('event_condition.event_type 是必填字段')

    if 'case_id' in value and (not isinstance(value['case_id'], int) or isinstance(value['case_id'], bool)):
        raise serializers.ValidationError('event_condition.case_id 必须是整数')

    if 'next_event' in value and (not isinstance(value['next_event'], str) or not value['next_event']):
        raise serializers.ValidationError('event_condition.next_event 必须是非空字符串')

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
        read_only_fields = ('created_at', 'updated_at', 'version', 'status')

