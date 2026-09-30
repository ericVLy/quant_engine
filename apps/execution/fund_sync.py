"""账户资金同步：按 gm 账户查询结果管理账户总资金 / 可用资金 / 持仓市值。

背景
----
``AccountFundConfig.total_capital`` 原本只能由管理员在后台手工填写，是"Plan 占用
资金"的总额上限。手工值会与券商真实资产漂移（出入金、持仓盈亏、其它终端下单），
导致 Plan 额度校验失真。本模块把 gm 账户查询结果归一后写回该行，使资金上限跟随
真实账户资产。

gm 账户查询契约（``GmBrokerAdapter.get_account`` → ``gm.api.get_cash``）
--------------------------------------------------------------------
====================  ============================================
字段                   含义
====================  ============================================
``balance``            账面资金（现金余额）
``market_value``       持仓市值
``available``          可用资金
``frozen``             冻结资金
``order_frozen``       委托冻结
``nav``                净值（基金类账户的总权益）
``currency``           币种
====================  ============================================

总资产口径
----------
``total_assets = balance + market_value``（股票账户：账面资金 + 持仓市值）。
两者都缺失而 ``nav`` 存在时（基金类账户）退化为 ``nav``。

额度口径（``capital_basis``）
---------------------------
账户里可能存在**本项目未管理的持仓**，其市值随行情盘中波动。若把这类市值计入
"Plan 可占用额度"的上限，额度会忽高忽低：持仓下跌时上限可能短暂低于已分配额度之和，
导致编辑既有 Plan 误报"超过账户空闲资金"。因此：

- ``total``（默认）：``balance + market_value``——账户总资产；
- ``cash``：只取 ``balance``——**存在外部持仓时推荐**；
- ``available``：只取 gm ``available``——最保守。

另有一条不变式：写入的额度**不低于该账户已分配额度之和**（``max(口径值, Σ已分配)``），
保证同步不会追溯性地作废既有额度分配。

安全约定
--------
- **失败绝不写 0**：查询异常或返回空数据一律抛 :class:`FundSyncError`，
  保留上一次成功同步的数值——把总资金误归零会让额度校验失效；
- 写库使用 ``select_for_update``，与资金链路（``state_machine`` / ``funds``）
  的行级锁口径一致；
- 同步**不触碰** ``FundAllocation``（Plan/Suite/Case 的已占用额度由运行时链路维护）；
- 日志中的账户 ID 统一脱敏（见 :func:`mask_account`，遵循 N-05 日志卫生）。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

__all__ = [
    'CAPITAL_BASES',
    'AccountSnapshot',
    'FundSyncError',
    'ensure_funds_fresh',
    'mask_account',
    'normalize_cash',
    'sync_account_funds',
    'sync_published_plan_accounts',
]

#: 额度口径（``AccountFundConfig.capital_basis``）：
#:
#: - ``total``：账户总资产（``balance + market_value``）；
#: - ``cash``：账面资金（忽略持仓市值）——**账户存在本项目未管理的持仓时推荐**，
#:   外部持仓市值盘中波动不会影响本项目可部署额度；
#: - ``available``：券商可用资金（最保守，只算当前真正能买的）。
CAPITAL_BASES = ('total', 'cash', 'available')


class FundSyncError(RuntimeError):
    """账户资金同步失败（保持上次成功同步的数值，不写 0）。"""


def mask_account(account_id):
    """账户 ID 脱敏（N-05：账户 ID 不进日志明文）。"""
    text = str(account_id or '')
    if len(text) <= 8:
        return text or '(空)'
    return f'{text[:4]}…{text[-4:]}'


@dataclass
class AccountSnapshot:
    """一次账户资金查询的归一结果（金额为 ``Decimal``，缺失为 ``None``）。"""

    account_id: str
    total_assets: Decimal | None = None
    balance: Decimal | None = None
    available_cash: Decimal | None = None
    market_value: Decimal | None = None
    frozen_cash: Decimal | None = None
    currency: str = ''

    def as_dict(self) -> dict[str, Any]:
        """JSON 安全表示（``Decimal`` → 字符串）。"""
        return {
            'account_id': self.account_id,
            'total_assets': None if self.total_assets is None else str(self.total_assets),
            'balance': None if self.balance is None else str(self.balance),
            'available_cash': None if self.available_cash is None else str(self.available_cash),
            'market_value': None if self.market_value is None else str(self.market_value),
            'frozen_cash': None if self.frozen_cash is None else str(self.frozen_cash),
            'currency': self.currency,
        }

    def capital_by_basis(self, basis='total') -> Decimal | None:
        """按额度口径取出用于 ``total_capital`` 的金额。

        Args:
            basis: ``total``（账面资金 + 持仓市值）/ ``cash``（账面资金）/
                ``available``（券商可用资金）。

        Returns:
            Decimal | None: 该口径下的金额；该口径字段缺失时返回 ``None``
            （调用方据此回退到总资产口径）。
        """
        if basis == 'cash':
            return self.balance
        if basis == 'available':
            return self.available_cash
        return self.total_assets


def _to_decimal(value) -> Decimal | None:
    """宽松转 ``Decimal``：``None`` / 空串 / 非数值 → ``None``（不抛异常）。"""
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def normalize_cash(raw, account_id='') -> AccountSnapshot:
    """把 gm ``Cash`` dict 归一为 :class:`AccountSnapshot`（容忍缺字段/空 dict）。

    Args:
        raw: gm ``get_cash`` 返回的 dict；空 dict / ``None`` 也可传入。
        account_id: 账户 ID（用于回填 ``snapshot.account_id``）。

    Returns:
        AccountSnapshot: ``total_assets`` 为 ``None`` 表示无法推导总资产
        （调用方据此判定同步失败）。
    """
    data = raw if isinstance(raw, dict) else {}

    balance = _to_decimal(data.get('balance'))
    market_value = _to_decimal(data.get('market_value'))
    nav = _to_decimal(data.get('nav'))

    total_assets: Decimal | None
    if balance is not None or market_value is not None:
        total_assets = (balance or Decimal('0')) + (market_value or Decimal('0'))
    else:
        # 基金类账户可能只给净值
        total_assets = nav

    available = _to_decimal(data.get('available'))
    if available is None:
        available = _to_decimal(data.get('cash'))

    frozen = _to_decimal(data.get('frozen'))
    order_frozen = _to_decimal(data.get('order_frozen'))
    frozen_cash = (
        None if frozen is None and order_frozen is None
        else (frozen or Decimal('0')) + (order_frozen or Decimal('0'))
    )

    return AccountSnapshot(
        account_id=str(account_id or data.get('account_id') or ''),
        total_assets=total_assets,
        balance=balance,
        available_cash=available,
        market_value=market_value,
        frozen_cash=frozen_cash,
        currency=str(data.get('currency') or ''),
    )


def _bind_account(broker, account_id) -> None:
    """把查询适配器绑定到目标账户（gm 要求先 ``set_account_id``）。

    gm 的 ``get_cash`` / ``get_position`` 在未绑定账户时可能报
    ``无效的ACCOUNT_ID``（status 1020），因此每次查询前都要确保适配器已绑定
    到当前账户——批量同步多个账户时尤其需要逐个重新绑定。

    绑定失败不抛异常：部分适配器（如测试替身）不提供 ``set_account_id``，
    此时沿用未绑定查询，由后续的 :func:`sync_account_funds` 统一判定成败。
    """
    if getattr(broker, 'account_id', None) == account_id:
        return
    binder = getattr(broker, 'set_account_id', None)
    if not callable(binder):
        return
    try:
        binder(account_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug('账户 %s 绑定失败（继续尝试未绑定查询）：%s',
                     mask_account(account_id), exc)


def _allocated_total(account_id) -> Decimal:
    """该账户下所有 Plan 的 ``allocated_capital`` 之和（已承诺占用的额度）。"""
    from django.db.models import Sum

    from apps.plans.models import Plan

    total = Plan.objects.filter(account_id=account_id).aggregate(
        total=Sum('allocated_capital'))['total']
    return total or Decimal('0')


@transaction.atomic
def sync_account_funds(account_id, broker, source='gm', capital_basis='total') -> dict[str, Any]:
    """按 gm 账户查询结果同步（upsert）``AccountFundConfig``。

    Args:
        account_id: 交易账户 ID。
        broker: 提供 ``get_account()`` 的适配器（``GmBrokerAdapter``）。
        source: 资金来源标记（``manual`` / ``gm``）。
        capital_basis: 额度口径（见 :data:`CAPITAL_BASES`）。账户存在
            **本项目未管理的持仓**时，盘中市值波动会让总资产口径忽高忽低，
            建议改用 ``cash``（只看账面资金）或 ``available``（最保守）。

    Returns:
        dict: 同步结果摘要（``Decimal`` → 字符串），含实际生效的口径
        ``capital_basis``、口径原始值 ``computed_capital``、已分配额度
        ``allocated_capital`` 与是否被下限托住 ``clamped``。

    Raises:
        FundSyncError: 缺少账户 ID / 通道、口径非法，或 gm 未返回可推导的资金
            数据（此时**不会**修改已有配置行）。
    """
    from .models import AccountFundConfig

    if not account_id:
        raise FundSyncError('缺少账户 ID，无法同步资金')
    if broker is None:
        raise FundSyncError('未配置账户查询通道，无法同步资金')
    if capital_basis not in CAPITAL_BASES:
        raise FundSyncError(f'capital_basis 必须是 {sorted(CAPITAL_BASES)} 之一')

    _bind_account(broker, account_id)
    try:
        raw = broker.get_account()
    except Exception as exc:  # pylint: disable=broad-except
        raise FundSyncError(f'账户 {mask_account(account_id)} 查询失败: {exc}') from exc

    snapshot = normalize_cash(raw, account_id)
    if snapshot.total_assets is None:
        raise FundSyncError(
            f'账户 {mask_account(account_id)} 未返回可推导的资金数据'
            '（balance / market_value / nav 均为空），保持上次同步值'
        )

    # 口径取值；该口径字段缺失时回退到总资产口径
    computed = snapshot.capital_by_basis(capital_basis)
    effective_basis = capital_basis
    if computed is None:
        computed, effective_basis = snapshot.total_assets, 'total'
    if computed is None:
        raise FundSyncError(
            f'账户 {mask_account(account_id)} 无法按口径 {capital_basis} 得到额度'
            '（且总资产同样不可得），保持上次同步值'
        )

    now = timezone.now()
    try:
        config = AccountFundConfig.objects.select_for_update().get(account_id=account_id)
        created = False
    except AccountFundConfig.DoesNotExist:
        config = AccountFundConfig(account_id=account_id)
        created = True

    # 不变式：额度上限不得低于"已分配额度之和"。账户里的外部持仓在盘中下跌时，
    # 口径值可能短暂低于既有 Plan 的已分配额度；若照单全收，编辑既有 Plan 会
    # 误报"超过账户空闲资金"。取 max 保证同步不追溯性地作废既有额度分配。
    allocated = _allocated_total(account_id)
    # 先在原始精度上判断"是否被已分配额度托底"，再量化到 2 位小数：
    # gm 返回值普遍带小数位（如 997655.9999847412），若量化后再比较会把
    # "四舍五入" 误报成 "托底"。
    raw_total = max(computed, allocated)
    clamped = raw_total != computed
    # 量化到 2 位小数，与落库列（decimal_places=2）保持一致，
    # 使命令/日志输出的额度与后台展示完全相同。
    total_capital = raw_total.quantize(Decimal('0.01'))

    config.total_capital = total_capital
    config.available_cash = snapshot.available_cash
    config.market_value = snapshot.market_value
    config.frozen_cash = snapshot.frozen_cash
    config.source = source
    config.capital_basis = effective_basis
    config.synced_at = now
    config.save()

    if clamped:
        logger.info(
            '账户 %s 口径值 %s 低于已分配额度 %s，额度下限托底为 %s（口径 %s）',
            mask_account(account_id), computed, allocated, total_capital, effective_basis,
        )
    logger.info(
        '账户 %s 资金已同步：额度=%s（口径 %s）可用=%s 持仓市值=%s 冻结=%s%s',
        mask_account(account_id), total_capital, effective_basis, snapshot.available_cash,
        snapshot.market_value, snapshot.frozen_cash, '（新建配置行）' if created else '',
    )
    result = snapshot.as_dict()
    result.update({
        'created': created,
        'synced_at': now.isoformat(),
        'source': source,
        'capital_basis': effective_basis,
        'total_capital': str(total_capital),
        'computed_capital': str(computed),
        'allocated_capital': str(allocated),
        'clamped': clamped,
    })
    return result


def ensure_funds_fresh(account_id, broker, ttl_seconds=30, source='gm',
                       capital_basis='total') -> dict[str, Any]:
    """TTL 保护的同步：数据够新则跳过，避免每次执行都打 gm 接口。

    Args:
        account_id: 交易账户 ID。
        broker: 账户查询适配器；``None`` 表示未启用资金同步。
        ttl_seconds: 有效期（秒）；``<= 0`` 表示每次都同步。
        source: 资金来源标记。
        capital_basis: 额度口径（见 :data:`CAPITAL_BASES`）。

    Returns:
        dict: ``{'synced': bool, ...}``；``synced=False`` 时带 ``reason``。
    """
    from .models import AccountFundConfig

    if not account_id or broker is None:
        return {'synced': False, 'reason': '未绑定账户或未启用资金同步'}

    config = AccountFundConfig.objects.filter(account_id=account_id).first()
    if config is not None and config.synced_at is not None and config.source == source:
        age = (timezone.now() - config.synced_at).total_seconds()
        if ttl_seconds and ttl_seconds > 0 and age < ttl_seconds:
            return {
                'synced': False,
                'reason': '资金数据仍在有效期内',
                'age_seconds': round(age, 1),
                'total_capital': str(config.total_capital),
            }
    result = sync_account_funds(
        account_id, broker, source=source, capital_basis=capital_basis)
    result['synced'] = True
    return result


def sync_published_plan_accounts(broker, source='gm',
                                 capital_basis='total') -> list[dict[str, Any]]:
    """批量同步"已发布 Plan 所引用的账户"（调度器周期性调用）。

    单个账户失败只记录结果、不影响其他账户（资金数据不会被写 0）。

    Args:
        broker: 账户查询适配器。
        source: 资金来源标记。
        capital_basis: 额度口径（见 :data:`CAPITAL_BASES`）。

    Returns:
        list[dict]: 每个账户一条结果（失败时含 ``error``）。
    """
    from apps.plans.models import Plan

    if broker is None:
        return []
    account_ids = list(
        Plan.objects.filter(status='published').exclude(account_id='')
        .values_list('account_id', flat=True).distinct()
    )
    results = []
    for account_id in account_ids:
        try:
            results.append(sync_account_funds(
                account_id, broker, source=source, capital_basis=capital_basis))
        except FundSyncError as exc:
            logger.warning('账户 %s 资金同步失败（保持上次值）：%s', mask_account(account_id), exc)
            results.append({'account_id': mask_account(account_id), 'error': str(exc)})
    return results
