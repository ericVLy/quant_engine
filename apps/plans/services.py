from django.db import transaction

from apps.execution.models import ExecutionLog, SuiteRun
from apps.suites.models import Suite
from apps.watchlists.models import Symbol
from apps.watchlists.services import case_symbol_scope, resolve_symbol_scope

from .models import Plan
from .models import PlanVersion


class PlanError(Exception):
    """Raised when a Plan cannot be published or triggered."""


#: Plan 级风控限额字段。
#: 与 ``runner.risk.PLAN_RISK_FIELDS`` 一一对应——这里刻意**不复用**该常量：
#: 按架构约定 ``apps.plans`` 不得依赖 ``runner``（``publish_plan`` 里的
#: ``PlanRegistry`` 也是函数内延迟导入），因此两侧各存一份字段名，修改时需同步。
_RISK_FIELDS = (
    'risk_position_mode', 'risk_max_order_volume', 'risk_max_order_value',
    'risk_max_daily_value', 'risk_max_account_value',
    'risk_max_position_value', 'risk_max_position_volume', 'risk_allowed_sessions',
)

#: 参与发布快照的 Plan 字段。风控限额必须在其中：否则"回滚了策略、风控还是新的"，
#: 会出现策略与限额不一致的危险状态。
_SNAPSHOT_FIELDS = (
    'name', 'root_suite_id', 'trigger_type', 'cron_expr', 'event_type',
    'exec_mode', 'retry_policy', 'status', 'version',
) + _RISK_FIELDS

#: 回滚时写入的模型字段名（注意是 ``root_suite`` 而非 ``root_suite_id``）
_ROLLBACK_FIELDS = (
    'name', 'root_suite', 'trigger_type', 'cron_expr', 'event_type',
    'exec_mode', 'retry_policy', 'status', 'version', 'updated_at',
) + _RISK_FIELDS

#: 回滚时由代码显式决定、不从快照恢复的字段
_ROLLBACK_EXCLUDE = ('root_suite_id', 'status', 'version')

#: 风控里的金额字段（快照里以字符串存放，回滚时需还原为 Decimal）
_RISK_DECIMAL_FIELDS = (
    'risk_max_order_value', 'risk_max_daily_value',
    'risk_max_account_value', 'risk_max_position_value',
)


def _json_safe(value):
    """把快照值归一为 JSON 可序列化形式（``Decimal`` → 字符串）。

    ``PlanVersion.snapshot`` 是 JSONField，直接放入 ``Decimal`` 会在保存时抛
    ``Object of type Decimal is not JSON serializable``（SQLite 与 MariaDB 都会失败）。
    金额以字符串存放既保住精度，也与 DRF 序列化 Decimal 的行为一致。
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _take_snapshot(plan):
    """按 :data:`_SNAPSHOT_FIELDS` 生成可安全入库的版本快照。"""
    return _json_safe({key: getattr(plan, key) for key in _SNAPSHOT_FIELDS})


def publish_plan(plan):
    """Publish a Plan only when its root Suite is already published.

    标的范围已下沉到 Case：发布时额外校验编排树内**至少有一个已发布 Case
    声明了 ``symbol_scope``**，否则该 Plan 触发后不会有任何标的被执行。

    Raises:
        PlanError: 根 Suite 未发布，或树内没有任何 Case 声明标的范围。
    """
    if plan.root_suite.status != 'published':
        raise PlanError('根 Suite 必须已发布')
    if not plan_declares_symbols(plan):
        raise PlanError(
            '编排树内没有任何已发布 Case 声明 symbol_scope；'
            '标的范围改由 Case 管理（params.symbol_scope），请先为至少一个 Case 配置标的'
        )
    from runner.registry import PlanRegistry

    with transaction.atomic():
        plan.status = 'published'
        plan.version += 1
        plan.save(update_fields=('status', 'version', 'updated_at'))
        snapshot = _take_snapshot(plan)
        PlanVersion.objects.create(plan=plan, version=plan.version, snapshot=snapshot)
        transaction.on_commit(lambda: PlanRegistry.refresh(plan))
    return plan


def rollback_plan(plan, version):
    """Restore a historical snapshot as a new published Plan version."""
    try:
        target = PlanVersion.objects.get(plan=plan, version=version)
    except PlanVersion.DoesNotExist as exc:
        raise PlanError(f'Plan 版本不存在: {version}') from exc

    snapshot = target.snapshot or {}
    if not snapshot.get('root_suite_id'):
        raise PlanError('Plan 历史版本缺少 root_suite_id')
    if not plan_declares_symbols(plan):
        raise PlanError('编排树内没有任何已发布 Case 声明 symbol_scope，无法回滚发布')
    with transaction.atomic():
        plan = Plan.objects.select_for_update().select_related('root_suite').get(pk=plan.pk)
        root_suite_id = snapshot['root_suite_id']
        if not Suite.objects.filter(pk=root_suite_id, status='published').exists():
            raise PlanError('历史版本的根 Suite 未发布')
        # 风控限额与策略配置一并回滚：两者必须来自同一个版本，
        # 否则会出现「策略回滚了、风控还是新的」这种危险的不一致。
        # 快照里没有的键（历史版本）保持现值，不臆断为空。
        for key in _SNAPSHOT_FIELDS:
            if key in _ROLLBACK_EXCLUDE or key not in snapshot:
                continue
            setattr(plan, key, snapshot[key])
        # 快照里的金额是字符串，还原为 Decimal 以免内存对象类型漂移
        for key in _RISK_DECIMAL_FIELDS:
            if getattr(plan, key, None) is not None:
                from decimal import Decimal
                setattr(plan, key, Decimal(str(getattr(plan, key))))
        plan.root_suite_id = root_suite_id
        plan.status = 'published'
        plan.version += 1
        plan.save(update_fields=_ROLLBACK_FIELDS)
        new_snapshot = _take_snapshot(plan)
        PlanVersion.objects.create(
            plan=plan, version=plan.version, snapshot=new_snapshot,
        )
        from runner.registry import PlanRegistry
        transaction.on_commit(lambda: PlanRegistry.refresh(plan, force=True))
    return plan


def resolve_plan_symbols(plan):
    """解析一个 Plan 实际覆盖的标的集合。

    标的范围由 **Case** 声明（``Case.params['symbol_scope']``），Plan 不再持有
    ``symbol_scope``。本函数遍历 Plan 根 Suite 的整棵编排树，取所有**已发布**
    Case 声明范围的**并集**：

    - 树内没有任何 Case 声明标的 → 返回空集合（该 Plan 不会产生任何任务）；
    - 某个 Case 声明 ``{'type': 'all'}`` → 并集即全市场；
    - ``groups`` / ``symbols`` 按各 Case 声明求并集后去重。

    Args:
        plan: ``plans.models.Plan`` 实例。

    Returns:
        QuerySet: 去重后的 ``watchlists.Symbol`` 集合（按 code 排序）。

    Raises:
        ValueError: 某个 Case 的 ``symbol_scope`` 非法。
    """
    codes = set()
    for case in iter_plan_cases(plan):
        scope = case_symbol_scope(case)
        if not scope:
            continue
        for symbol in resolve_symbol_scope(scope).only('code'):
            codes.add(symbol.code)
    if not codes:
        return Symbol.objects.none()
    return Symbol.objects.filter(code__in=codes).order_by('code')


def iter_plan_cases(plan):
    """遍历 Plan 根 Suite 编排树中的全部 Case（含各层子 Suite 的 Case）。

    Args:
        plan: ``plans.models.Plan`` 实例。

    Yields:
        Case: 树内 Case 实例（顺序为深度优先，重复挂载不重复产出）。
    """
    root = plan.root_suite
    seen_suites = set()
    pending = [root]
    while pending:
        suite = pending.pop(0)
        if suite.pk in seen_suites:
            continue
        seen_suites.add(suite.pk)
        for case in suite.cases.all():
            yield case
        pending.extend(suite.children.all())


def plan_declares_symbols(plan) -> bool:
    """Plan 编排树内是否存在**已发布**且声明了标的范围的 Case。"""
    for case in iter_plan_cases(plan):
        if case.status == 'published' and case_symbol_scope(case):
            return True
    return False


def delete_plan(plan):
    """Delete a Plan only when it has no execution history."""
    if SuiteRun.objects.filter(plan=plan).exists():
        raise PlanError('Plan 已有执行记录，不能删除')
    if ExecutionLog.objects.filter(plan=plan).exists():
        raise PlanError('Plan 已有执行日志，不能删除')
    plan.delete()
