"""分时监控采样与清理服务（模块9）。"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.watchlists.models import Symbol

from .market_calendar import (
    in_trading_session, market_timezone, to_market_local, trading_minutes_local,
)
from .models import IntradayPoint

logger = logging.getLogger(__name__)

MARKETS = ('A', 'HK', 'US')


def _default_snapshot_provider():
    """默认分时数据源：gm SDK 为主源（A 股），akshare 回退（HK/US 及失败时）。"""
    from .snapshot_provider import (
        AkshareSpotProvider, CompositeSnapshotProvider, GmSnapshotProvider,
    )
    return CompositeSnapshotProvider([GmSnapshotProvider(), AkshareSpotProvider()])


def resolve_sample_symbols(markets=None, symbols=None):
    """确定采样标的集合。

    - ``symbols`` 显式给出代码列表时，仅返回库中存在且（可选）市场匹配的标的；
    - 缺省时取「已发布 Plan」的 symbol_scope 并集（设计按 Plan/自选池标的范围控制数据量）；
    - 没有已发布 Plan 时回退为全部标的，保证独立可用。
    """
    scope = Symbol.objects.all()
    if symbols:
        codes = [str(code).strip() for code in symbols if str(code).strip()]
        if not codes:
            return Symbol.objects.none()
        scope = Symbol.objects.filter(code__in=codes)
    else:
        published = _published_plan_symbols()
        if published.exists():
            scope = published
    if markets:
        market_list = [str(m).upper() for m in markets]
        scope = scope.filter(market__in=market_list)
    return scope.distinct()


def _published_plan_symbols():
    """已发布 Plan 标的范围的并集；无 Plan 时返回空 QuerySet。"""
    from apps.plans.models import Plan
    from apps.watchlists.services import resolve_symbol_scope

    codes = set()
    for plan in Plan.objects.filter(status='published'):
        try:
            for symbol in resolve_symbol_scope(plan.symbol_scope).only('code'):
                codes.add(symbol.code)
        except Exception:  # pylint: disable=broad-except
            logger.warning('解析 Plan %s 标的范围失败，跳过', plan.pk)
            continue
    if not codes:
        return Symbol.objects.none()
    return Symbol.objects.filter(code__in=codes).distinct()


def _to_decimal(value, default=None):
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _match_symbol(code, wanted):
    """按返回代码匹配库中标的（容错：A股补前导零 / US 大小写）。"""
    if code in wanted:
        return wanted[code]
    code = str(code or '').strip()
    candidates = [code]
    if code.isdigit():
        candidates.append(code.zfill(6))
    candidates += [code.upper(), code.lower()]
    for candidate in candidates:
        if candidate in wanted:
            return wanted[candidate]
    return None


def sample_intraday(provider=None, markets=None, symbols=None, now=None):
    """执行一轮分时采样，返回按市场分组的摘要。

    - 按 ``market`` 分组标的，仅对处于交易时段的 market 执行（``in_trading_session``）；
    - 同一 ``(symbol, ts)`` 使用 ``update_or_create`` 覆盖（分钟级幂等）；
    - provider 失败只记录该市场错误，不中断其他市场。
    """
    provider = provider or _default_snapshot_provider()
    now = now if now is not None else timezone.now()
    ts = now.replace(second=0, microsecond=0)

    by_market = {}
    for symbol in resolve_sample_symbols(markets=markets, symbols=symbols):
        by_market.setdefault(symbol.market, []).append(symbol)

    summary = {}
    for market, symbol_list in by_market.items():
        info = {'sampled': 0, 'skipped': 0, 'failed': []}
        summary[market] = info
        if not in_trading_session(market, to_market_local(now, market)):
            info['skipped'] += len(symbol_list)
            continue
        try:
            quotes = provider.fetch_market(market, symbol_list)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning('[%s] 快照拉取失败: %s', market, exc)
            info['failed'].append(str(exc))
            continue

        wanted = {symbol.code: symbol for symbol in symbol_list}
        for code, item in (quotes or {}).items():
            symbol = _match_symbol(code, wanted)
            if symbol is None or item.get('price') is None:
                continue
            IntradayPoint.objects.update_or_create(
                symbol=symbol,
                ts=ts,
                defaults={
                    'price': _to_decimal(item.get('price')),
                    'change': _to_decimal(item.get('change'), Decimal('0')),
                    'volume': int(float(item['volume'])) if item.get('volume') is not None else 0,
                    'amount': _to_decimal(item.get('amount')),
                    'avg_price': None,
                    'high': _to_decimal(item.get('high')),
                    'low': _to_decimal(item.get('low')),
                    'open_price': _to_decimal(item.get('open_price')),
                    'pre_close': _to_decimal(item.get('pre_close')),
                },
            )
            info['sampled'] += 1
    return summary


def clear_intraday(before=None, now=None):
    """删除 ``ts < before`` 的分时记录；缺省 ``before`` 为当日 00:00 UTC。

    幂等：没有满足条件记录时删除 0 条，不报错。
    """
    now = now if now is not None else timezone.now()
    if before is None:
        before = now.replace(hour=0, minute=0, second=0, microsecond=0)
    deleted, _ = IntradayPoint.objects.filter(ts__lt=before).delete()
    return deleted
def _to_utc_minutes(minutes_local, market):
    """把 market 本地 naive 分钟列表转成 aware UTC 分钟列表。"""
    tz = market_timezone(market)
    return [
        m.replace(tzinfo=tz).astimezone(ZoneInfo('UTC')).replace(second=0, microsecond=0)
        for m in minutes_local
    ]


def _missing_minutes(symbol, target_utc):
    """目标分钟中缺失（IntradayPoint 尚不存在）的部分，按升序返回。"""
    if not target_utc:
        return []
    existing = set(
        IntradayPoint.objects
        .filter(symbol=symbol, ts__range=(target_utc[0], target_utc[-1]))
        .values_list('ts', flat=True),
    )
    existing = {t.replace(second=0, microsecond=0) for t in existing}
    return [m for m in target_utc if m not in existing]


def _accumulate_bar(state, bar):
    """把逐分钟 bar 累积到当日快照状态（open/high/low/volume/amount）。"""
    if state['open'] is None and bar.get('open') is not None:
        state['open'] = bar['open']
    high, low = bar.get('high'), bar.get('low')
    if high is not None and (state['high'] is None or high > state['high']):
        state['high'] = high
    if low is not None and (state['low'] is None or low < state['low']):
        state['low'] = low
    if bar.get('volume') is not None:
        state['volume'] += Decimal(str(bar['volume']))
    if bar.get('amount') is not None:
        state['amount'] += Decimal(str(bar['amount']))
    return state


def _backfill_symbol(symbol, missing_set, bars):
    """用逐分钟 bar 回填缺失分钟（累计成交量/成交额、日内高低、开盘价、change）。"""
    pre_close = next((b.get('pre_close') for b in bars if b.get('pre_close') is not None), None)
    state = {'open': None, 'high': None, 'low': None, 'volume': Decimal('0'), 'amount': Decimal('0')}
    filled = 0
    for bar in sorted(bars, key=lambda b: b['ts'] if b.get('ts') is not None else datetime.min):
        _accumulate_bar(state, bar)
        ts = bar.get('ts')
        if ts is None or ts not in missing_set:
            continue
        price = bar.get('close')
        change = None
        if price is not None and pre_close:
            change = round((price - pre_close) / pre_close * 100.0, 4)
        IntradayPoint.objects.update_or_create(
            symbol=symbol,
            ts=ts,
            defaults={
                'price': _to_decimal(price),
                'change': _to_decimal(change, Decimal('0')),
                'volume': int(state['volume']),
                'amount': _to_decimal(state['amount']),
                'avg_price': None,
                'high': _to_decimal(state['high']),
                'low': _to_decimal(state['low']),
                'open_price': _to_decimal(state['open']),
                'pre_close': _to_decimal(pre_close),
            },
        )
        filled += 1
    return filled


def _fetch_history(provider, market, symbol, start, end):
    """向 provider 拉取逐分钟历史；不支持/失败时返回 []（回填静默跳过）。"""
    try:
        return provider.fetch_intraday_history(market, symbol, start, end) or []
    except NotImplementedError:
        return []
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning('[%s/%s] 回填历史拉取失败: %s', market, symbol.code, exc)
        return []


def backfill_intraday(provider=None, markets=None, symbols=None, now=None):
    """启动时完整性回填：检查并从开盘到 now 补全缺失的交易分钟点。

    - 按 ``market`` 分组，周末 / 开盘前跳过；
    - 目标 = 当日已开启的交易分钟（``trading_minutes_local``），已有点不覆盖；
    - 缺失分钟向 provider 取逐分钟历史（``fetch_intraday_history``），按快照语义写入
      （累计成交量/成交额、日内最高/最低、开盘价、change）；
    - provider 不支持历史（如 akshare HK/US）时静默跳过，不影响其他标的。
    """
    provider = provider or _default_snapshot_provider()
    now = now if now is not None else timezone.now()

    by_market = {}
    for symbol in resolve_sample_symbols(markets=markets, symbols=symbols):
        by_market.setdefault(symbol.market, []).append(symbol)

    summary = {}
    for market, symbol_list in by_market.items():
        info = {'checked': 0, 'complete': 0, 'backfilled': 0, 'missing_remaining': []}
        summary[market] = info
        now_local = to_market_local(now, market)
        target = _to_utc_minutes(trading_minutes_local(market, now_local), market)
        if not target:
            continue
        if in_trading_session(market, now_local) and len(target) > 1:
            # 盘中：当前分钟 bar 尚未生成，不计入完整性目标（下一轮自然补上）
            target = target[:-1]
        for symbol in symbol_list:
            info['checked'] += 1
            missing = _missing_minutes(symbol, target)
            if not missing:
                info['complete'] += 1
                continue
            # gm history 按 bar 结束时间（eob）过滤 end_time，因此 end 需多加一分钟
            bars = _fetch_history(provider, market, symbol, target[0], target[-1] + timedelta(minutes=1))
            bar_ts = {b.get('ts') for b in bars if b.get('ts') is not None}
            missing_set = {m for m in missing if m in bar_ts}
            info['backfilled'] += _backfill_symbol(symbol, missing_set, bars)
            info['missing_remaining'].extend(m for m in missing if m not in bar_ts)
    return summary