from rest_framework import serializers

from apps.cases.models import Case

from .models import Edge, Suite


class EdgeSerializer(serializers.ModelSerializer):
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

