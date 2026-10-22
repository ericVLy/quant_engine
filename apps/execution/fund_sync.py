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
import os
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

__all__ = [
    'CAPITAL_BASES',
    'AccountSnapshot',
    'PositionSnapshot',
    'FundSyncError',
    'ensure_funds_fresh',
    'mask_account',
    'normalize_cash',
    'normalize_positions',
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


@dataclass
class PositionSnapshot:
    """一次账户持仓查询的归一结果。

    持仓明细仅用于**展示与外部持仓判定**，不参与风控计算（风控仍以实时账户
    快照为准，见 ``runner.risk.RiskController``）。
    """

    account_id: str
    positions: list[dict[str, Any]] = None  # type: ignore[assignment]
    total_volume: Decimal = Decimal('0')
    total_market_value: Decimal = Decimal('0')
    external_symbols: list[str] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        """JSON 安全表示（``Decimal`` → 字符串）。"""
        return {
            'account_id': self.account_id,
            'positions': self.positions or [],
            'position_count': len(self.positions or []),
            'total_volume': str(self.total_volume),
            'total_market_value': str(self.total_market_value),
            'external_symbols': self.external_symbols or [],
            'has_external_position': bool(self.external_symbols),
        }


def _position_market_value(item: dict[str, Any]) -> Decimal | None:
    """取持仓市值：gm 返回 ``market_value``，部分版本给 ``value``。

    与 ``runner.risk`` 的字段回退顺序保持一致，避免两处口径不同。
    """
    for key in ('market_value', 'value'):
        if key in item:
            got = _to_decimal(item.get(key))
            if got is not None:
                return got
    return None


def _position_price(item: dict[str, Any]) -> Decimal | None:
    """取持仓价格：``closep`` / ``price`` / ``cost_price``（gm 各版本命名不一）。"""
    for key in ('closep', 'price', 'cost_price'):
        got = _to_decimal(item.get(key))
        if got is not None:
            return got
    return None


def _canonical_symbol(symbol: str) -> str:
    """把标的代码归一为可比较的形式（用于外部持仓判定）。

    gm 返回交易所前缀形式（``SHSE.600000`` / ``SZSE.000001``），而本系统
    ``Symbol.code`` 存纯数字或``sh``/``sz`` 前缀形式（``600000`` / ``sh000001``），
    两者直接比较会把**全部**持仓误判为外部持仓。故统一去掉交易所段与分隔符，
    只保留数字部分再比较。
    """
    text = str(symbol or '').strip().upper()
    if '.' in text:                      # SHSE.600000 → 600000
        text = text.rsplit('.', 1)[-1]
    for prefix in ('SH', 'SZ', 'BJ'):    # sh000001 → 000001
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.lstrip('.').strip()


def normalize_positions(raw, account_id='', known_symbols=None) -> PositionSnapshot:
    """把 gm 持仓列表归一为 :class:`PositionSnapshot`。

    Args:
        raw: gm ``get_position`` 返回的 list；``None`` 也可传入（视为空仓）。
        account_id: 账户 ID（gm user id），用于回填。
        known_symbols: **本系统已纳管**的标的代码集合。用于判定外部持仓——
            不在其中的持仓即"本项目未管理"，其市值盘中波动会让 ``total`` 口径
            额度忽高忽低，故据此提示改用 ``cash`` 口径。两侧代码会经
            :func:`_canonical_symbol` 归一后再比较（gm 带 ``SHSE.`` 前缀而
            系统存纯数字）。``None`` 或空集合表示跳过判定。

    Returns:
        PositionSnapshot: 归一后的持仓；``total_market_value`` 为 0 且明细为空
        表示空仓或 gm 未返回持仓数据（两者对额度计算影响一致）。
    """
    items = raw if isinstance(raw, (list, tuple)) else []
    known: set[str] = set()
    if known_symbols:
        known = {_canonical_symbol(s) for s in known_symbols if str(s).strip()}

    normalized: list[dict[str, Any]] = []
    total_volume = Decimal('0')
    total_market_value = Decimal('0')
    external: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get('symbol') or '').strip()
        if not symbol:
            continue
        # 空仓占位（volume<=0）不算持仓
        volume = _to_decimal(item.get('volume', item.get('quantity'))) or Decimal('0')
        if volume <= 0:
            continue
        price = _position_price(item)
        market_value = _position_market_value(item)
        if market_value is None:
            market_value = (price or Decimal('0')) * volume
        total_volume += volume
        total_market_value += market_value
        # 无known 集合时不做外部判定（无法判断纳管范围）
        is_external = bool(known) and _canonical_symbol(symbol) not in known
        if is_external:
            external.append(symbol)
        normalized.append({
            'symbol': symbol,
            'volume': str(volume),
            'price': None if price is None else str(price),
            'market_value': str(market_value),
            'is_external': is_external,
        })

    return PositionSnapshot(
        account_id=str(account_id or ''),
        positions=normalized,
        total_volume=total_volume,
        total_market_value=total_market_value,
        external_symbols=sorted(set(external)),
    )


def _sync_timeout_seconds():
    """单账户同步的**总超时**（秒）。超过即放弃本次 gm 调用并按失败处理。

    为什么需要：gm 终端/网络异常时 ``get_cash`` / ``get_position`` 可能长时间
    不返回（SDK 未暴露超时参数）。

    ⚠️ 本函数**不能**替代进程隔离：实测 gm SDK 阻塞时**持有 GIL**，同进程内的
    主线程连 ``join`` 的超时逻辑都执行不到。Web 请求路径必须走子进程
    （见 ``apps.execution.views.run_account_sync_in_subprocess``）；
    此处的超时仅用于调度器等非 Web 进程，或作为 gm 释放 GIL 时的额外兜底。
    """
    configured = getattr(settings, 'ACCOUNT_SYNC_TIMEOUT_SECONDS', None)
    if configured is not None:
        return int(configured)
    return int(os.getenv('ACCOUNT_SYNC_TIMEOUT_SECONDS', '20'))


def _call_with_timeout(func, timeout, label, account_id):
    """在守护线程里执行 ``func``，超时则放弃并抛 :class:`FundSyncError`。

    Args:
        func: 无参可调用（如 ``broker.get_account``）。
        timeout: 等待秒数；``<= 0`` 表示不限制（不推荐）。
        label: 日志/报错用的操作名。
        account_id: 账户 ID（报错脱敏用）。

    Returns:
        func 的返回值。

    Raises:
        FundSyncError: 超时，或 func 自身抛出的异常（统一包装）。
    """
    box = {}

    def runner():
        try:
            box['value'] = func()
        except BaseException as exc:  # pylint: disable=broad-except
            box['error'] = exc

    worker = threading.Thread(target=runner, daemon=True,
                              name=f'gm-sync-{mask_account(account_id)}')
    worker.start()
    worker.join(timeout if timeout and timeout > 0 else None)
    if worker.is_alive():
        raise FundSyncError(
            f'账户 {mask_account(account_id)} {label} 超过 {timeout}s 未返回，已放弃'
            '（请检查 gm 终端连接与网络）'
        )
    if 'error' in box:
        raise box['error']
    return box.get('value')


def _bind_account(broker, account_id) -> None:
    """把查询适配器指向目标账户（记录 ``broker.account_id``）。

    ⚠️ 这里**只记录逻辑账户**，绝不触碰 gm SDK 的 ``api.set_account_id()``。

    实测（真实 gm SDK，2026-10-22）：``api.set_account_id()`` 一旦在进程内被
    调用，后续所有 ``api.get_cash()`` 都会**永久死锁**（C 扩展持有 GIL，
    连 ``join(timeout)`` 的超时逻辑都执行不到 → 整个进程冻结），
    且 ``api.set_account_id('')`` 清空预设也救不回来。
    分时监控（走 ``api.history``）不受影响，所以现象是"行情正常、资金查询卡死"。

    查询类接口（``get_cash`` / ``get_position``）本来就支持**显式传
    account_id**，实测耗时 0.17s / 0.00s，根本不需要预设账户。

    早期版本这里"必须先绑定"的注释已过时：那是针对更老 SDK 的行为，
    在当前 SDK 上恰恰是死锁的直接原因。

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


def _resolve_capital(computed, allocated):
    """按"额度不低于已分配额度之和"的不变式算出最终额度。

    Returns:
        tuple[Decimal, bool]: ``(total_capital, clamped)``；``clamped`` 表示口径值
        被已分配额度托底（量化前比较，避免把"四舍五入"误报成托底）。
    """
    raw_total = max(computed, allocated)
    clamped = raw_total != computed
    # 量化到 2 位小数，与落库列（decimal_places=2）一致，使日志/页面展示完全相同
    return raw_total.quantize(Decimal('0.01')), clamped


def _fetch_remote_snapshot(account_id, broker, capital_basis, with_positions):
    """执行**全部 gm 网络 I/O**并返回归一结果。

    ⚠️ 必须在本函数返回后才允许开启数据库事务。

    原因：``dev`` 配置的 SQLite 使用 ``transaction_mode=IMMEDIATE``，
    ``BEGIN IMMEDIATE`` 会在事务**入口**就抢占**库级写锁**。若把
    ``broker.get_account()`` / ``get_positions()`` 放在事务内，这个写锁会被
    整个网络往返时长（gm 终端无响应时可达数十秒）一直占着；由于 SQLite
    全库只有一个写者，**任何其它用户的写请求都会阻塞到``timeout``（默认 30s）
    甚至直接报``database is locked``**，表现为"同步一失败/卡住，整站都打不开"。
    把网络 I/O 与事务拆开，写锁只在真正落库的那几毫秒内被持有。

    Args:
        account_id: gm user id。
        broker: 账户查询适配器。
        capital_basis: 额度口径。
        with_positions: 是否一并拉取持仓。

    Returns:
        tuple: ``(snapshot, position_snapshot, computed, effective_basis)``；
        ``position_snapshot`` 为 ``None`` 表示持仓不可用（不影响资金）。

    Raises:
        FundSyncError: gm 查询失败或未返回可推导的资金数据。
    """
    _bind_account(broker, account_id)
    try:
        raw = _call_with_timeout(broker.get_account, _sync_timeout_seconds(),
                                 '资金查询', account_id)
    except FundSyncError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise FundSyncError(f'账户 {mask_account(account_id)} 查询失败: {exc}') from exc

    snapshot = normalize_cash(raw, account_id)
    if snapshot.total_assets is None:
        raise FundSyncError(
            f'账户 {mask_account(account_id)} 未返回可推导的资金数据'
            '（balance / market_value / nav 均为空），保持上次同步值'
        )

    # 持仓快照（失败不影响资金同步结果）
    # 注意：此处**不再**重复调_bind_account——上面查询资金时已绑定过，
    # 同一账户重复绑定既多余（多一次 set_account_id 调用）又会让绑定计数类断言失真。
    position_snapshot = _sync_positions_quietly(account_id, broker) if with_positions else None

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
    return snapshot, position_snapshot, computed, effective_basis


@transaction.atomic
def _persist_account_funds(*, account_id, snapshot, position_snapshot, computed,
                           effective_basis, source):
    """把已取到的资金/持仓快照落库。**纯写库，不做任何网络 I/O。**"""
    from .models import AccountFundConfig

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
    total_capital, clamped = _resolve_capital(computed, allocated)

    config.total_capital = total_capital
    config.available_cash = snapshot.available_cash
    config.market_value = snapshot.market_value
    config.frozen_cash = snapshot.frozen_cash
    config.source = source
    config.capital_basis = effective_basis
    config.synced_at = now
    if position_snapshot is not None:
        _apply_positions(config, position_snapshot, now)
    config.save()

    return created, allocated, total_capital, clamped, now


def sync_account_funds(account_id, broker, source='gm', capital_basis='total',
                       with_positions=True) -> dict[str, Any]:
    """按 gm 账户查询结果同步（upsert）``AccountFundConfig``。

    **结构约束**：网络 I/O（:func:`_fetch_remote_snapshot`）与数据库事务
    （:func:`_persist_account_funds`）必须分离——SQLite 在
    ``transaction_mode=IMMEDIATE`` 下会在事务入口抢占库级写锁，若把 gm
    网络往返放进事务，一次卡住的同步会让**全站写请求**阻塞至超时，
    表现为"同步失败时整站打不开"。详见 :func:`_fetch_remote_snapshot`。

    Args:
        account_id: gm user id（交易账户 ID）。
        broker: 提供 ``get_account()`` / ``get_positions()`` 的适配器
            （``GmBrokerAdapter``）。
        source: 资金来源标记（``manual`` / ``gm``）。
        capital_basis: 额度口径（见 :data:`CAPITAL_BASES`）。账户存在
            **本项目未管理的持仓**时，盘中市值波动会让总资产口径忽高忽低，
            建议改用 ``cash``（只看账面资金）或 ``available``（最保守）。
        with_positions: 是否同步持仓快照。持仓查询失败**不影响**资金同步
            （资金是额度上限的主依据，持仓仅用于展示与口径提示）。

    Returns:
        dict: 同步结果摘要（``Decimal`` → 字符串），含实际生效的口径
        ``capital_basis``、口径原始值 ``computed_capital``、已分配额度
        ``allocated_capital``、是否被下限托住 ``clamped``，以及
        ``positions`` 持仓快照摘要。

    Raises:
        FundSyncError: 缺少账户 ID / 通道、口径非法，或 gm 未返回可推导的资金
            数据（此时**不会**修改已有配置行）。
        OperationalError: 落库阶段遇到数据库锁竞争（调用方应归一为可读错误，
            而不是 500）。
    """
    if not account_id:
        raise FundSyncError('缺少账户 ID，无法同步资金')
    if broker is None:
        raise FundSyncError('未配置账户查询通道，无法同步资金')
    if capital_basis not in CAPITAL_BASES:
        raise FundSyncError(f'capital_basis 必须是 {sorted(CAPITAL_BASES)} 之一')

    # ① 网络 I/O —— 事务外，此时**不持有**任何数据库锁
    snapshot, position_snapshot, computed, effective_basis = _fetch_remote_snapshot(
        account_id, broker, capital_basis, with_positions)

    # ② 落库 —— 短事务，只包住这几毫秒的读写
    created, allocated, total_capital, clamped, now = _persist_account_funds(
        account_id=account_id, snapshot=snapshot, position_snapshot=position_snapshot,
        computed=computed, effective_basis=effective_basis, source=source)

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
    return _build_result(snapshot, position_snapshot, created=created, synced_at=now,
                         source=source, effective_basis=effective_basis,
                         total_capital=total_capital, computed=computed,
                         allocated=allocated, clamped=clamped)


def _apply_positions(config, position_snapshot: PositionSnapshot, now) -> None:
    """把持仓快照写入账户配置行（``config`` 由调用方save）。"""
    config.position_count = len(position_snapshot.positions)
    config.position_volume = position_snapshot.total_volume
    config.positions = position_snapshot.positions
    config.position_symbols = sorted({p['symbol'] for p in position_snapshot.positions})
    config.has_external_position = bool(position_snapshot.external_symbols)
    config.external_position_symbols = position_snapshot.external_symbols
    config.position_synced_at = now


def _build_result(snapshot, position_snapshot, *, created, synced_at, source,
                  effective_basis, total_capital, computed, allocated, clamped):
    """组装同步结果摘要（``Decimal`` → 字符串，可安全 JSON 化）。"""
    result = snapshot.as_dict()
    result.update({
        'created': created,
        'synced_at': synced_at.isoformat(),
        'source': source,
        'capital_basis': effective_basis,
        'total_capital': str(total_capital),
        'computed_capital': str(computed),
        'allocated_capital': str(allocated),
        'clamped': clamped,
        'positions': position_snapshot.as_dict() if position_snapshot else None,
    })
    if position_snapshot and position_snapshot.external_symbols:
        # 外部持仓会让 total 口径额度随行情波动，提示改用 cash（仅提示，不自动改）
        result['basis_suggestion'] = 'cash'
    return result


def _sync_positions_quietly(account_id, broker) -> PositionSnapshot | None:
    """查询并归一持仓；任何异常都降级为 ``None``（不阻断资金同步）。

    调用方需已把broker 绑定到 ``account_id``（``sync_account_funds`` 在查资金时已绑定）。

    持仓数据缺失时保留上一次快照——与资金"失败绝不写 0"的约定同源：
    把持仓误清空会让"外部持仓"提示消失，诱导用户误用 ``total`` 口径。
    """
    try:
        raw = _call_with_timeout(broker.get_positions, _sync_timeout_seconds(),
                                 '持仓查询', account_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning('账户 %s 持仓查询失败（保留上次快照）：%s',
                       mask_account(account_id), exc)
        return None
    try:
        from apps.watchlists.models import Symbol
        known = set(Symbol.objects.values_list('code', flat=True))
    except Exception:  # pylint: disable=broad-except  # pragma: no cover
        known = set()
    return normalize_positions(raw, account_id, known_symbols=known or None)


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
