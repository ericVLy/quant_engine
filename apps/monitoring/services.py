"""分时监控采样与清理服务（模块9）。"""
import logging
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.watchlists.models import Symbol

from .market_calendar import in_trading_session, to_market_local
from .models import IntradayPoint
from .snapshot_provider import AkshareSpotProvider

logger = logging.getLogger(__name__)

MARKETS = ('A', 'HK', 'US')


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
    provider = provider or AkshareSpotProvider()
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
            quotes = provider.fetch_market(market)
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