# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
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


def validate_risk_amount(value, label):
    """风控金额字段：必须是 > 0 的有限 Decimal（``None`` 表示不限制）。"""
    if value in (None, ''):
        return None
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise serializers.ValidationError(f'{label}必须是数值') from exc
    if not amount.is_finite() or amount <= 0:
        raise serializers.ValidationError(f'{label}必须大于 0')
    return amount


def validate_risk_volume(value, label):
    """风控数量字段：必须是 > 0 的整数（拒绝布尔伪装；``None`` 表示不限制）。"""
    if value in (None, ''):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise serializers.ValidationError(f'{label}必须是大于 0 的整数')
    if value <= 0:
        raise serializers.ValidationError(f'{label}必须大于 0')
    return value


def _normalize_session(item, index):
    """把单个时段窗口规整成 ``[起始时, 起始分, 结束时, 结束分]``。"""
    if not isinstance(item, (list, tuple)):
        raise serializers.ValidationError(f'risk_allowed_sessions[{index}] 必须是数组')
    values = list(item)
    if len(values) == 2 and all(isinstance(v, (list, tuple)) for v in values):
        values = [v for pair in values for v in pair]      # [[9,30],[11,30]] → 4 元
    if len(values) != 4:
        raise serializers.ValidationError(
            f'risk_allowed_sessions[{index}] 必须是 4 个整数或两个 [时,分] 二元组')
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise serializers.ValidationError(
                f'risk_allowed_sessions[{index}] 只能包含整数（时/分）')
    start_h, start_m, end_h, end_m = values
    for label, hour, minute in (('起始', start_h, start_m), ('结束', end_h, end_m)):
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise serializers.ValidationError(
                f'risk_allowed_sessions[{index}] 的{label}时间越界（时 0-23，分 0-59）')
    if (start_h, start_m) >= (end_h, end_m):
        raise serializers.ValidationError(
            f'risk_allowed_sessions[{index}] 的起始时间必须早于结束时间')
    return [start_h, start_m, end_h, end_m]


def validate_risk_allowed_sessions(value):
    """交易时段窗口：接受扁平 ``[[h,m,h,m]]`` 或嵌套 ``[[[h,m],[h,m]]]``，统一存扁平形式。"""
    if value in (None, '', []):
        return None
    if not isinstance(value, (list, tuple)):
        raise serializers.ValidationError('risk_allowed_sessions 必须是数组')
    return [_normalize_session(item, index) for index, item in enumerate(value)]


class PlanSerializer(serializers.ModelSerializer):
    available_capital = serializers.SerializerMethodField()
    account_status = serializers.SerializerMethodField(help_text='账户匹配状态与提示，供前端提示用户')

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

    def get_account_status(self, obj):
        """该 Plan 绑定账户的匹配状态（供前端提示，不参与校验）。

        返回 ``{configured, is_active, available_capital, basis_suggestion, warning}``；
        ``warning`` 在"额度口径与外部持仓不匹配"时给出可读提示。
        """
        from apps.execution.models import AccountFundConfig

        if not obj.account_id:
            return {'configured': False, 'is_active': False, 'available_capital': None,
                    'basis_suggestion': '', 'warning': ''}
        cfg = AccountFundConfig.objects.filter(account_id=obj.account_id).first()
        if cfg is None:
            return {
                'configured': False, 'is_active': False, 'available_capital': None,
                'basis_suggestion': '',
                'warning': '账户未预配置，无法校验占用资金',
            }
        warning = ''
        if not cfg.is_active:
            warning = '账户已停用，无法为其分配占用资金'
        elif cfg.has_external_position and cfg.capital_basis == 'total':
            warning = (
                f'该账户存在外部持仓（{len(cfg.external_position_symbols)} 只，'
                '市值随行情波动），建议将额度口径改为「账面资金」以免额度忽高忽低'
            )
        return {
            'configured': True,
            'is_active': cfg.is_active,
            'available_capital': str(cfg.available_capital),
            'basis_suggestion': cfg.basis_suggestion,
            'warning': warning,
        }

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

    def validate_retry_policy(self, value):
        try:
            return validate_retry_policy(value)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(exc.detail) from exc

    # ---- Plan 级风控限额（F1）----
    # 说明：这里**只保留一个** ``validate`` 方法。历史上本类曾定义过两个同名
    # ``validate``（一个含资金占用校验、一个只做触发器校验），Python 中后者会
    # 静默覆盖前者，导致 ``validate_plan_capital`` 成为死代码、"Plan 占用资金
    # 不得超过账户空闲资金"的规则实际不生效。新增字段时一律挂 ``validate_<field>``。
    def validate_risk_max_order_value(self, value):
        return validate_risk_amount(value, '单笔金额上限')

    def validate_risk_max_daily_value(self, value):
        return validate_risk_amount(value, '每日累计金额上限')

    def validate_risk_max_account_value(self, value):
        return validate_risk_amount(value, '账户可用资金上限')

    def validate_risk_max_position_value(self, value):
        return validate_risk_amount(value, '总仓位金额上限')

    def validate_risk_max_order_volume(self, value):
        return validate_risk_volume(value, '单笔数量上限')

    def validate_risk_max_position_volume(self, value):
        return validate_risk_volume(value, '总仓位数量上限')

    def validate_risk_allowed_sessions(self, value):
        return validate_risk_allowed_sessions(value)
