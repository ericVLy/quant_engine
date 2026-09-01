from rest_framework import serializers
from .models import Symbol, Group, Watchlist
from .services import resolve_symbol_name


class SymbolSerializer(serializers.ModelSerializer):
    name = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Symbol
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def validate(self, attrs):
        code = attrs.get('code') or getattr(self.instance, 'code', None)
        market = attrs.get('market') or getattr(self.instance, 'market', None)
        name = attrs.get('name')

        if code and not name:
            resolved_name = resolve_symbol_name(code, market)
            if resolved_name:
                attrs['name'] = resolved_name

        if not attrs.get('name'):
            raise serializers.ValidationError({'name': '名称不能为空，或请提供代码与市场以自动填充名称。'})

        return attrs


class GroupSerializer(serializers.ModelSerializer):
    symbols = SymbolSerializer(many=True, read_only=True)
    symbol_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = Group
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def create(self, validated_data):
        symbol_ids = validated_data.pop('symbol_ids', [])
        group = Group.objects.create(**validated_data)
        if symbol_ids:
            group.symbols.set(symbol_ids)
        return group

    def update(self, instance, validated_data):
        symbol_ids = validated_data.pop('symbol_ids', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if symbol_ids is not None:
            instance.symbols.set(symbol_ids)
        return instance


class WatchlistSerializer(serializers.ModelSerializer):
    groups = GroupSerializer(many=True, read_only=True)
    group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = Watchlist
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'user')

    def create(self, validated_data):
        group_ids = validated_data.pop('group_ids', [])
        user = self.context['request'].user
        watchlist = Watchlist.objects.create(user=user, **validated_data)
        if group_ids:
            watchlist.groups.set(group_ids)
        return watchlist

    def update(self, instance, validated_data):
        group_ids = validated_data.pop('group_ids', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if group_ids is not None:
            instance.groups.set(group_ids)
        return instance