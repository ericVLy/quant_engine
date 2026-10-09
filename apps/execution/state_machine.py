"""Case / Suite / Plan 运行状态机。"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用

from decimal import Decimal

from django.db import transaction

from apps.suites.models import Suite
from apps.plans.models import Plan


class StateMachineError(Exception):
    """状态流转校验失败。"""


def start_case(case):
    """Case 进入 running。Case 依托 Suite 运行。"""
    _assert_case_runnable(case)
    case.run_status = 'running'
    case.save(update_fields=['run_status', 'updated_at'])
    return case


def complete_case(case):
    """Case 执行成功 → done。"""
    if case.run_status != 'running':
        raise StateMachineError(f'Case {case.pk} 未在运行，无法标记完成')
    case.run_status = 'done'
    case.save(update_fields=['run_status', 'updated_at'])
    _try_complete_suite(case)
    return case


def fail_case(case):
    """Case 执行失败 → failed；所属 Suite 进入 interrupt。"""
    if case.run_status != 'running':
        raise StateMachineError(f'Case {case.pk} 未在运行，无法标记失败')
    case.run_status = 'failed'
    case.save(update_fields=['run_status', 'updated_at'])
    _interrupt_suite_for_case(case)
    return case


def _assert_case_runnable(case):
    suite = Suite.objects.filter(cases=case).first()
    if suite is None:
        raise StateMachineError(f'Case {case.pk} 未加入任何 Suite，无法运行')
    if suite.run_status != 'running':
        raise StateMachineError(
            f'Case {case.pk} 所属 Suite {suite.pk} 状态为 {suite.run_status}，无法运行'
        )
    return suite


def _try_complete_suite(case):
    suite = Suite.objects.filter(cases=case).first()
    if suite is None or suite.run_status != 'running':
        return
    statuses = set(suite.cases.values_list('run_status', flat=True))
    if statuses <= {'done'}:
        complete_suite(suite)


def _interrupt_suite_for_case(case):
    suite = Suite.objects.filter(cases=case).first()
    if suite is not None and suite.run_status == 'running':
        interrupt_suite(suite, reason='case_failed')


def start_suite(suite):
    """Suite 进入 running。所属 Plan 必须为 running。"""
    _assert_suite_runnable(suite)
    suite.run_status = 'running'
    suite.save(update_fields=['run_status', 'updated_at'])
    return suite


def complete_suite(suite):
    """Suite 执行完成 → done。旗下 cases 全部 done 后自动触发。"""
    if suite.run_status not in ('running', 'new'):
        raise StateMachineError(f'Suite {suite.pk} 状态为 {suite.run_status}，无法标记完成')
    suite.run_status = 'done'
    suite.save(update_fields=['run_status', 'updated_at'])
    _try_complete_plan(suite)
    return suite


def interrupt_suite(suite, reason="manual"):
    """Suite 进入 interrupt（手动停止 / case 失败）。"""
    if suite.run_status not in ('running', 'new'):
        raise StateMachineError(f'Suite {suite.pk} 状态为 {suite.run_status}，无法中断')
    for case in suite.cases.filter(run_status='running'):
        case.run_status = 'failed'
        case.save(update_fields=['run_status', 'updated_at'])
    suite.run_status = 'interrupt'
    suite.save(update_fields=['run_status', 'updated_at'])
    return suite


def _assert_suite_runnable(suite):
    plan = _resolve_plan_for_suite(suite)
    if plan is None:
        raise StateMachineError(f'Suite {suite.pk} 未加入任何 Plan，无法运行')
    if plan.run_status != 'running':
        raise StateMachineError(
            f'Suite {suite.pk} 所属 Plan {plan.pk} 状态为 {plan.run_status}，无法运行'
        )
    return plan


def _resolve_plan_for_suite(suite):
    plan = Plan.objects.filter(root_suite=suite).first()
    if plan:
        return plan
    current = suite
    seen = set()
    while current.parent_id is not None and current.parent_id not in seen:
        seen.add(current.parent_id)
        current = current.parent
    return Plan.objects.filter(root_suite=current).first()


def _try_complete_plan(suite):
    plan = _resolve_plan_for_suite(suite)
    if plan is None or plan.run_status != 'running':
        return
    all_suites = _collect_plan_suites(plan)
    statuses = set(s.run_status for s in all_suites)
    if statuses <= {'done'}:
        complete_plan(plan)


def _collect_plan_suites(plan):
    result = []
    pending = [plan.root_suite] if plan.root_suite else []
    seen = set()
    while pending:
        suite = pending.pop()
        if suite is None or suite.pk in seen:
            continue
        seen.add(suite.pk)
        result.append(suite)
        pending.extend(suite.children.all())
    return result


def start_plan(plan):
    """Plan 进入 running。自动启动模式下同时启动根 Suite。

    允许从 ``new`` / ``done`` / ``interrupt`` 启动——即跑完之后可以**再次驱动**
    （归一后 Plan 会变成 ``done`` / ``interrupt``，此前是"一次性"的）；仅拒绝
    ``running``（已在运行中，重复启动只会造成并发执行）。
    """
    if plan.run_status not in ('new', 'done', 'interrupt'):
        raise StateMachineError(f'Plan {plan.pk} 状态为 {plan.run_status}，无法启动')
    plan.run_status = 'running'
    plan.save(update_fields=['run_status', 'updated_at'])
    if plan.suite_start_mode == 'auto' and plan.root_suite:
        start_suite(plan.root_suite)
    return plan


def stop_plan(plan):
    """Plan 手动停止 → interrupt。如有运行的 suite，强制停止。"""
    if plan.run_status != 'running':
        raise StateMachineError(f'Plan {plan.pk} 未在运行，无法停止')
    for suite in _collect_plan_suites(plan):
        if suite.run_status == 'running':
            interrupt_suite(suite, reason='plan_stopped')
    plan.run_status = 'interrupt'
    plan.save(update_fields=['run_status', 'updated_at'])
    return plan


def complete_plan(plan):
    """Plan 执行完成 → done。"""
    if plan.run_status not in ('running', 'new'):
        raise StateMachineError(f'Plan {plan.pk} 状态为 {plan.run_status}，无法标记完成')
    plan.run_status = 'done'
    plan.save(update_fields=['run_status', 'updated_at'])
    return plan


@transaction.atomic
def validate_plan_capital(plan):
    """Plan 创建/更新时校验：按 gm user id 匹配预配置账户，且占用 ≤ 账户空闲资金。

    ``plan.account_id`` 存**预配置的 gm user id**，与 ``AccountFundConfig``（账户
    预配置表）按 ``account_id`` 精确匹配：

    - 未登记该 id → 提示先去账户管理里预配置；
    - 已登记但停用（``is_active=False``）→ 拒绝，避免把额度分配到不投用的账户；
    - 已同步 gm 资金但持仓里有**外部标的**且仍用 ``total`` 口径 → 给出提醒
      （不阻断：改口径属业务决策，只提示外部持仓市值会令额度随行情波动）。

    使用 select_for_update 对 AccountFundConfig 行加锁，
    保证校验 + 占用在同一事务内原子完成，消除并发 race condition。
    """
    if not plan.account_id or not plan.allocated_capital:
        return
    from .fund_sync import mask_account
    from .models import AccountFundConfig

    masked = mask_account(plan.account_id)
    try:
        cfg = AccountFundConfig.objects.select_for_update().get(account_id=plan.account_id)
    except AccountFundConfig.DoesNotExist:
        # 缺配置记录是控制流信号，原异常（DoesNotExist）对使用者无信息量，
        # 显式抑制异常链，与 mcp_server/mutations.py 的同类处理保持一致。
        raise StateMachineError(
            f'账户 {masked} 未预配置，无法占用资金；请先在「账户管理」中登记该 gm user id'
        ) from None

    if not cfg.is_active:
        raise StateMachineError(f'账户 {masked} 已停用，无法为其分配占用资金')

    used = Plan.objects.filter(account_id=plan.account_id).exclude(
        pk=plan.pk,
    ).aggregate(total=_sum('allocated_capital'))['total'] or Decimal('0')
    available = cfg.total_capital - used
    if plan.allocated_capital > available:
        raise StateMachineError(
            f'Plan 占用资金 {plan.allocated_capital} 超过账户 {masked} 空闲资金 {available}'
            f'（总资金 {cfg.total_capital}，已占用 {used}）'
        )


@transaction.atomic
def validate_suite_joining_plan(suite, plan):
    """Suite 加入 Plan 时校验：Plan 必须有足够的空闲资金。

    使用 select_for_update 对 Plan 级 FundAllocation 行加锁，
    保证校验 + 分配在同一事务内原子完成，消除并发 race condition。
    """
    if not suite.allocated_capital:
        return
    if not plan.allocated_capital:
        raise StateMachineError(f'Plan {plan.pk} 未设置占用资金，无法加入 Suite')
    from .models import FundAllocation
    plan_alloc = FundAllocation.objects.filter(
        plan=plan, level='plan', status='active',
    ).first()
    if plan_alloc is None:
        raise StateMachineError(f'Plan {plan.pk} 未配置 plan 级资金额度')
    # 对 Plan 级额度行加锁，防止并发加入时超分配
    FundAllocation.objects.select_for_update().get(pk=plan_alloc.pk)
    all_suites = _collect_plan_suites(plan)
    used = sum(
        Decimal(str(s.allocated_capital)) for s in all_suites
        if s.allocated_capital and s.pk != suite.pk
    )
    available = Decimal(str(plan.allocated_capital)) - used
    if suite.allocated_capital > available:
        raise StateMachineError(
            f'Suite 占用资金 {suite.allocated_capital} 超过 Plan 空闲资金 {available}'
            f'（Plan 总额 {plan.allocated_capital}，已分配 {used}）'
        )


def _sum(field):
    from django.db.models import Sum
    return Sum(field)
