from rest_framework import serializers
from .models import DataSource, RealtimeSnapshot, KLineSyncLog
from apps.watchlists.models import Symbol


class DataSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataSource
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')


class SymbolField(serializers.PrimaryKeyRelatedField):
    """
    自定义字段：序列化时返回 symbol 的详细信息（id, code, name, market）
    反序列化时接受 symbol 的 ID
    """
    def to_representation(self, value):
        # value 可能是 Symbol 实例或 PKOnlyObject
        if hasattr(value, 'pk'):
            obj_id = value.pk
        else:
            obj_id = value

        # 如果是 PKOnlyObject，需要从数据库获取对象
        if not isinstance(value, Symbol):
            try:
                obj = Symbol.objects.get(pk=obj_id)
            except Symbol.DoesNotExist:
                return {'id': obj_id, 'code': None, 'name': None, 'market': None}
        else:
            obj = value

        return {
            'id': obj.id,
            'code': obj.code,
            'name': obj.name,
            'market': obj.market
        }

    def to_internal_value(self, data):
        # 父类处理，接受 ID
        return super().to_internal_value(data)


class RealtimeSnapshotSerializer(serializers.ModelSerializer):
    symbol = SymbolField(queryset=Symbol.objects.all())

    class Meta:
        model = RealtimeSnapshot
        fields = '__all__'
        read_only_fields = ('updated_at',)


class KLineSyncLogSerializer(serializers.ModelSerializer):
    symbol = SymbolField(queryset=Symbol.objects.all())

    class Meta:
        model = KLineSyncLog
        fields = '__all__'
        read_only_fields = ('created_at',)


class KLineSerializer(serializers.Serializer):
    """K线查询结果的序列化器，统一字段"""
    symbol = serializers.CharField()
    date = serializers.DateField()
    open = serializers.DecimalField(max_digits=12, decimal_places=4)
    high = serializers.DecimalField(max_digits=12, decimal_places=4)
    low = serializers.DecimalField(max_digits=12, decimal_places=4)
    close = serializers.DecimalField(max_digits=12, decimal_places=4)
    volume = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=20, decimal_places=2, allow_null=True)
    extra = serializers.JSONField(required=False, allow_null=True)