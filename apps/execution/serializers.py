# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from rest_framework import serializers
from .models import (SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order, FundAllocation,
                    Alert, AlertChannel, NodeRun, AccountFundConfig)
from .funds import FundError, allocate_funds
from .registry import EventRegistry


class EventTypeRegistrySerializer(serializers.ModelSerializer):
    is_active = serializers.BooleanField(default=True)

    class Meta:
        model = EventTypeRegistry
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def validate_name(self, value):
        """确保新事件类型不与内置事件冲突"""
        from .events import EventType
        if value in EventType.all():
            raise serializers.ValidationError(f"'{value}' 是系统内置事件，不允许重复注册")
        return value

    def validate(self, attrs):
        """叠加约束：用户自定义事件只能叠加在系统自带事件之上。

        - scope='user'：base_event_type 必填，且必须是系统内置事件
          （不允许以其他用户/插件注册的事件为基，防止叠加套娃）；
        - scope='plugin'：base_event_type 可选；提供时同样必须是系统内置事件；
        - scope='system'：系统内置事件由代码定义，禁止通过注册表 API 冒充创建。
        """
        from .events import EventType

        scope = attrs.get('scope') or getattr(self.instance, 'scope', 'user')
        if 'base_event_type' in attrs:
            base = attrs.get('base_event_type') or None
        else:
            base = getattr(self.instance, 'base_event_type', None) or None

        if scope == 'system':
            if self.instance is None:
                raise serializers.ValidationError(
                    "系统内置事件由代码定义（events.EventType），不允许创建 scope='system' 的事件类型"
                )
            return attrs

        if scope == 'user' and not base:
            raise serializers.ValidationError(
                "用户自定义事件必须叠加在系统自带事件之上：请指定 base_event_type 为系统内置事件"
            )
        if base and not EventType.is_valid(base):
            raise serializers.ValidationError(
                f"叠加基事件 '{base}' 不是系统自带事件；"
                f"仅支持叠加系统自带事件（如 {', '.join(EventType.all()[:6])} 等）"
            )
        return attrs


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        fields = '__all__'
        read_only_fields = ('created_at',)

    def validate_event_type(self, value):
        """校验事件类型是否已注册"""
        if not EventRegistry.validate(value):
            raise serializers.ValidationError(f"未注册的事件类型: {value}")
        return value


class SuiteRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = SuiteRun
        fields = '__all__'
        read_only_fields = ('created_at',)


class NodeRunSerializer(serializers.ModelSerializer):
    """节点级运行实例序列化器（执行轨迹回放数据源）。"""
    node_type_display = serializers.CharField(source='get_node_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    suite_name = serializers.CharField(source='suite.name', read_only=True, allow_null=True, default=None)
    case_name = serializers.CharField(source='case.name', read_only=True, allow_null=True, default=None)
    symbol = serializers.CharField(source='run.symbol', read_only=True)

    class Meta:
        model = NodeRun
        fields = (
            'id', 'run', 'parent', 'node_type', 'node_type_display',
            'suite', 'suite_name', 'case', 'case_name', 'symbol',
            'status', 'status_display', 'direction', 'result',
            'started_at', 'ended_at',
        )
        read_only_fields = fields


class ExecutionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionLog
        fields = '__all__'


class AccountFundConfigSerializer(serializers.ModelSerializer):
    """gm 账户预配置：登记 gm user id、启用状态与额度口径，并回传资金/持仓快照。

    写入侧（``account_id`` / ``display_name`` / ``is_active`` / ``capital_basis`` /
    ``remark``）由管理员维护；资金与持仓字段全部**只读**——它们只能由
    ``fund_sync.sync_account_funds`` 从 gm 同步写入，避免前端误改导致额度失真。
    """

    allocated_capital = serializers.SerializerMethodField()
    available_capital = serializers.SerializerMethodField()
    basis_suggestion = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    is_stale = serializers.BooleanField(read_only=True)
    masked_account_id = serializers.SerializerMethodField()

    class Meta:
        model = AccountFundConfig
        fields = (
            'id', 'account_id', 'display_name', 'remark', 'is_active', 'label',
            'total_capital', 'source', 'capital_basis', 'basis_suggestion',
            'available_cash', 'market_value', 'frozen_cash', 'synced_at', 'is_stale',
            'position_count', 'position_volume', 'positions', 'position_symbols',
            'has_external_position', 'external_position_symbols', 'position_synced_at',
            'allocated_capital', 'available_capital', 'masked_account_id',
        )
        # 资金/持仓快照只能由 gm 同步写入，不接受前端写入
        read_only_fields = (
            'total_capital', 'source', 'available_cash', 'market_value', 'frozen_cash',
            'synced_at', 'position_count', 'position_volume', 'positions',
            'position_symbols', 'has_external_position', 'external_position_symbols',
            'position_synced_at',
        )

    def get_allocated_capital(self, obj):
        return str(obj.allocated_capital)

    def get_available_capital(self, obj):
        return str(obj.available_capital)

    def get_masked_account_id(self, obj):
        """账户 ID 脱敏（N-05：账户 ID 不进日志/普通响应明文）。"""
        from .fund_sync import mask_account
        return mask_account(obj.account_id)

    def validate_account_id(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('gm user id 不能为空')
        return value

    def validate(self, attrs):
        """停用账户前必须先解除其 Plan 占用，否则这些 Plan 的额度校验会悬空。"""
        is_active = attrs.get('is_active', getattr(self.instance, 'is_active', True))
        if self.instance is not None and is_active is False:
            bound = self.instance.plan_count
            if bound:
                raise serializers.ValidationError(
                    f'该账户仍被 {bound} 个 Plan 引用，请先调整这些 Plan 的账户或清空占用资金后再停用'
                )
        return attrs


class FundAllocationSerializer(serializers.ModelSerializer):
    """分级资金申请：Plan 占用 → Suite 申请 → Case 申请，层级校验由
    ``funds.allocate_funds`` 统一执行。"""

    class Meta:
        model = FundAllocation
        fields = '__all__'
        read_only_fields = ('used_amount', 'status', 'level', 'created_at', 'updated_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # validate() 阶段已原子完成资金占用，create()/update() 只需回传该结果
        self._allocation = None

    def validate(self, attrs):
        plan = attrs.get('plan') or getattr(self.instance, 'plan', None)
        suite = attrs.get('suite') or getattr(self.instance, 'suite', None)
        case = attrs.get('case') or getattr(self.instance, 'case', None)
        amount = attrs.get('amount') or getattr(self.instance, 'amount', None)
        if plan is None:
            raise serializers.ValidationError('plan 必填')
        if amount is None:
            raise serializers.ValidationError('amount 必填')
        if case is not None and suite is None:
            raise serializers.ValidationError('case 级资金申请必须同时指定 suite')
        try:
            self._allocation = allocate_funds(plan, suite=suite, case=case, amount=amount)
        except FundError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return attrs

    def create(self, validated_data):
        return self._allocation

    def update(self, instance, validated_data):
        return self._allocation


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = '__all__'


class AlertSerializer(serializers.ModelSerializer):
    """告警序列化器"""
    alert_type_display = serializers.CharField(source='get_alert_type_display', read_only=True)
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True, allow_null=True)
    suite_run_display = serializers.CharField(source='suite_run.__str__', read_only=True, allow_null=True)

    class Meta:
        model = Alert
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'in_app_notified', 'email_notified', 'notification_error')


class AlertChannelSerializer(serializers.ModelSerializer):
    """告警渠道配置序列化器"""
    channel_type_display = serializers.CharField(source='get_channel_type_display', read_only=True)
    min_severity_display = serializers.CharField(source='get_min_severity_display', read_only=True)

    class Meta:
        model = AlertChannel
        # 显式白名单（弃用 '__all__'）：渠道配置含收件人邮箱等 PII，新增字段须显式放行（N-05）
        fields = (
            'id', 'channel_type', 'channel_type_display', 'is_enabled',
            'email_recipients', 'email_subject_prefix',
            'min_severity', 'min_severity_display', 'alert_types',
            'created_at', 'updated_at',
        )
        read_only_fields = ('created_at', 'updated_at')

    def validate_alert_types(self, value):
        """验证告警类型列表"""
        if not isinstance(value, list):
            raise serializers.ValidationError("alert_types 必须是列表")

        valid_types = [choice[0] for choice in Alert.ALERT_TYPE_CHOICES]
        for alert_type in value:
            if alert_type not in valid_types:
                raise serializers.ValidationError(f"无效的告警类型: {alert_type}")

        return value

    def validate_email_recipients(self, value):
        """验证邮件收件人列表"""
        if not isinstance(value, list):
            raise serializers.ValidationError("email_recipients 必须是列表")

        for email in value:
            if not isinstance(email, str) or '@' not in email:
                raise serializers.ValidationError(f"无效的邮件地址: {email}")

        return value


class AlertActionSerializer(serializers.Serializer):  # pylint: disable=abstract-method  # 仅承载告警动作入参校验，不经 create/update 落库
    """告警操作序列化器"""
    action = serializers.ChoiceField(choices=['acknowledge', 'resolve'])
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)
