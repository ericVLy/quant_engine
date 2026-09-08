import math

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


def validate_retry_policy(value):
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError('retry_policy 必须是 JSON 对象')
    allowed = {'max_retries', 'delay_seconds'}
    unknown = set(value) - allowed
    if unknown:
        raise serializers.ValidationError(
            f'retry_policy 不允许的字段: {", ".join(sorted(unknown))}'
        )
    max_retries = value.get('max_retries', 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise serializers.ValidationError('retry_policy.max_retries 必须是大于等于 0 的整数')
    delay_seconds = value.get('delay_seconds', 0)
    if isinstance(delay_seconds, bool):
        raise serializers.ValidationError('retry_policy.delay_seconds 必须是非负有限数值')
    try:
        delay_seconds = float(delay_seconds)
    except (TypeError, ValueError) as exc:
        raise serializers.ValidationError('retry_policy.delay_seconds 必须是非负有限数值') from exc
    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise serializers.ValidationError('retry_policy.delay_seconds 必须是非负有限数值')
    return value


class PlanSerializer(serializers.ModelSerializer):
    available_capital = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'version', 'status', 'run_status')

    def get_available_capital(self, obj):
        if not obj.account_id or not obj.allocated_capital:
            return None
        from apps.execution.models import AccountFundConfig
        try:
            cfg = AccountFundConfig.objects.get(account_id=obj.account_id)
        except AccountFundConfig.DoesNotExist:
            return None
        return str(cfg.available_capital)

    def validate_allocated_capital(self, value):
        if value is None:
            return value
        from decimal import Decimal
        if Decimal(str(value)) <= 0:
            raise serializers.ValidationError('占用资金必须大于 0')
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

        # 资金校验
        account_id = attrs.get('account_id', getattr(self.instance, 'account_id', ''))
        allocated = attrs.get('allocated_capital', getattr(self.instance, 'allocated_capital', None))
        if account_id and allocated:
            plan = Plan(pk=self.instance.pk if self.instance else None, account_id=account_id,
                        allocated_capital=allocated)
            from apps.execution.state_machine import validate_plan_capital
            try:
                validate_plan_capital(plan)
            except Exception as exc:
                raise serializers.ValidationError({'allocated_capital': str(exc)}) from exc

        return attrs

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
            if any(isinstance(item, bool) or not isinstance(item, int) for item in value['group_ids']):
                raise serializers.ValidationError('group_ids 必须是整数数组')
            if set(value.keys()) - {'type', 'group_ids'}:
                raise serializers.ValidationError('groups 类型只允许 type 和 group_ids 字段')
        if scope_type == 'symbols':
            if not isinstance(value.get('symbol_codes'), list):
                raise serializers.ValidationError('symbols 类型必须提供 symbol_codes 数组')
            if any(not isinstance(item, str) or not item.strip() for item in value['symbol_codes']):
                raise serializers.ValidationError('symbol_codes 必须是非空字符串数组')
            if set(value.keys()) - {'type', 'symbol_codes'}:
                raise serializers.ValidationError('symbols 类型只允许 type 和 symbol_codes 字段')
        return value

    def validate_retry_policy(self, value):
        try:
            return validate_retry_policy(value)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(exc.detail) from exc

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
