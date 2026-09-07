"""分级资金申请与占用服务。

资金链路：**Plan 占用账户资金 → Suite 向 Plan 申请 → Case 向 Suite 申请**。

- 申请（allocate）：逐级校验父额度存在且子级申请总额不超过父额度；
- 扣减（reserve_for_order）：下单时按 case → suite → plan 就近扣减
  ``used_amount``，行级锁（select_for_update）保证并发安全；
- 回退（refund）：订单拒单/撤单时释放已占用金额。

未配置任何资金额度时，运行时不做资金拦截（向后兼容既有行为）。
"""
from decimal import Decimal

from django.db import transaction

from .models import FundAllocation


class FundError(Exception):
    """资金申请/占用校验失败。"""


class InsufficientFunds(FundError):
    """剩余额度不足以支付当前订单。"""


def _as_money(value):
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # pylint: disable=broad-except
        raise FundError('资金额度必须是有限数值') from exc
    if amount <= 0:
        raise FundError('资金额度必须大于 0')
    return amount.quantize(Decimal('0.01'))


def models_sum(field):
    from django.db.models import Sum
    return Sum(field)


@transaction.atomic
def allocate_funds(plan, suite=None, case=None, amount=None):
    """创建或更新一条分级资金申请，并执行层级总额校验。"""
    amount = _as_money(amount)
    if case is not None:
        if suite is None:
            raise FundError('case 级资金申请必须指定所属 Suite')
        parent = FundAllocation.objects.filter(
            plan=plan, suite=suite, level='suite', status='active',
        ).first()
        if parent is None:
            raise FundError('Suite 尚未向 Plan 申请资金，无法为 Case 分配额度')
        siblings = _sum_excluding(level='case', plan=plan, suite=suite, case=case)
        if siblings + amount > parent.amount:
            raise FundError(
                f'Case 申请 {amount} 超过 Suite 剩余额度 '
                f'（已申请 {siblings} / 总额 {parent.amount}）'
            )
        allocation, _ = FundAllocation.objects.update_or_create(
            plan=plan, suite=suite, case=case, level='case',
            defaults={'amount': amount, 'status': 'active'},
        )
        return allocation
    if suite is not None:
        parent = FundAllocation.objects.filter(
            plan=plan, level='plan', status='active',
        ).first()
        if parent is None:
            raise FundError('Plan 尚未设置占用资金（allocated_capital），无法为 Suite 申请')
        siblings = _sum_excluding(level='suite', plan=plan, suite=suite)
        if siblings + amount > parent.amount:
            raise FundError(
                f'Suite 申请 {amount} 超过 Plan 剩余额度 '
                f'（已申请 {siblings} / 总额 {parent.amount}）'
            )
        allocation, _ = FundAllocation.objects.update_or_create(
            plan=plan, suite=suite, case=None, level='suite',
            defaults={'amount': amount, 'status': 'active'},
        )
        return allocation
    plan_capital = plan.allocated_capital
    if plan_capital is None:
        raise FundError('Plan 必须先设置 allocated_capital 才能建立 plan 级资金额度')
    if amount > Decimal(str(plan_capital)):
        raise FundError(
            f'plan 级额度 {amount} 不得超过占用资金总额 {plan_capital}'
        )
    allocation, _ = FundAllocation.objects.update_or_create(
        plan=plan, suite=None, case=None, level='plan',
        defaults={'amount': amount, 'status': 'active'},
    )
    return allocation


def _sum_excluding(level, plan, suite=None, case=None):
    qs = FundAllocation.objects.filter(plan=plan, level=level, status='active')
    if level == 'suite':
        qs = qs.exclude(suite=suite)
    elif level == 'case':
        qs = qs.exclude(case=case)
    total = qs.aggregate(total=models_sum('amount'))['total'] or Decimal('0')
    return total


def release_funds(allocation):
    """释放资金额度（已占用金额需为 0 或强制释放）。"""
    allocation.status = 'released'
    allocation.save(update_fields=['status', 'updated_at'])
    return allocation


@transaction.atomic
def reserve_for_order(plan, suite=None, case=None, value=None):
    """下单占用：按 case → suite → plan 就近扣减，返回使用的 FundAllocation。

    - 未配置任何资金额度 → 返回 None（不拦截，保持旧行为）；
    - 配置了额度但剩余不足 → 抛出 InsufficientFunds。
    """
    order_value = _as_money(value)
    candidates = []
    if case is not None:
        candidates.append(
            FundAllocation.objects.filter(
                plan=plan, case=case, level='case', status='active',
            ).first()
        )
    if suite is not None:
        candidates.append(
            FundAllocation.objects.filter(
                plan=plan, suite=suite, case=None, level='suite', status='active',
            ).first()
        )
    candidates.append(
        FundAllocation.objects.filter(
            plan=plan, suite=None, case=None, level='plan', status='active',
        ).first()
    )
    allocation = next((item for item in candidates if item is not None), None)
    if allocation is None:
        return None
    configured = [item for item in candidates if item is not None]
    # 逐级尝试扣减：case → suite → plan；上一级剩余不足时回退下一级，
    # 全部配置层级都无法覆盖订单金额时才拒绝。
    last_remaining = Decimal('0')
    last_amount = last_used = None
    for item in configured:
        locked = FundAllocation.objects.select_for_update().get(pk=item.pk)
        remaining = locked.amount - locked.used_amount
        if order_value <= remaining:
            locked.used_amount = locked.used_amount + order_value
            locked.save(update_fields=['used_amount', 'updated_at'])
            return locked
        last_remaining, last_amount, last_used = remaining, locked.amount, locked.used_amount
    raise InsufficientFunds(
        f'资金额度不足：需要 {order_value}，最贴近层级剩余 {last_remaining}'
        f'（额度 {last_amount}，已占用 {last_used}）'
    )


@transaction.atomic
def refund(allocation, value):
    """订单被拒/撤单后回退占用金额。"""
    if allocation is None:
        return None
    order_value = Decimal(str(value or 0))
    locked = FundAllocation.objects.select_for_update().get(pk=allocation.pk)
    locked.used_amount = max(Decimal('0'), locked.used_amount - order_value)
    locked.save(update_fields=['used_amount', 'updated_at'])
    return locked
