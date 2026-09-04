"""为 Case 执行提供真实数据上下文（K线 / 实时快照 / 基本面）。

数据源优先级：

1. 本地数据库（``apps.datasources``）：优先查询已同步的 K 线分表与
   ``RealtimeSnapshot`` 实时快照，是最稳定、离线的数据来源；
2. gm SDK（``GmBrokerAdapter``）：作为在线行情回退，未落库时兜底。

``DataContextBuilder.build`` 返回统一结构，供 ``CaseExecutor`` 的因子计算使用。
"""

from datetime import datetime

from django.utils import timezone

from apps.datasources.models import RealtimeSnapshot
from apps.datasources.services import query_kline_table
from apps.watchlists.models import Symbol


def _db_kline(symbol, count=50, end_time=None):
    """从本地 K 线分表查询最近 ``count`` 条记录，返回 list[dict]。"""
    if end_time is None:
        end_date = datetime.now().date()
    elif hasattr(end_time, 'date'):
        end_date = end_time.date()
    else:
        end_date = end_time

    start_date = end_date
    try:
        rows = query_kline_table(symbol, start_date, end_date)
    except Exception:
        return []
    # 数据不足时向前扩大窗口重试
    attempts = 0
    while len(rows) < count and attempts < 5:
        start_date = start_date.replace(day=1)  # 向前扩到月初
        next_rows = query_kline_table(symbol, start_date, end_date)
        if not next_rows or len(next_rows) == len(rows):
            break
        rows = next_rows
        attempts += 1
    return rows[-count:]


class MarketDataFixture:
    """Provide SDK-backed market data context for Case execution."""

    def __init__(self, broker):
        self.broker = broker

    def load(self, symbol, frequency='1d', count=50, fields=None, end_time=None):
        return self.broker.history_n(
            symbol=symbol, frequency=frequency, count=count,
            end_time=end_time, fields=fields, data_frame=True,
        )

    def subscribe(self, symbol, frequency='1d', count=1, fields=None):
        return self.broker.subscribe(
            symbols=symbol, frequency=frequency, count=count, fields=fields,
        )

    def context(self, symbol, frequency='1d', count=50, fields=None, end_time=None):
        """Return a stable Case context without imposing a pandas dependency."""
        data = self.load(symbol, frequency, count, fields, end_time)
        return {'symbol': symbol, 'frequency': frequency, 'market_data': data}
class DataContextBuilder:
    """通过多数据源构建稳定、统一的 Case 执行上下文。"""

    def __init__(self, broker=None, prefer_db=True):
        self.broker = broker
        self.prefer_db = prefer_db

    def _snapshot(self, symbol):
        if symbol is None:
            return None
        snap = None
        try:
            snap = getattr(symbol, 'snapshot', None)
        except RealtimeSnapshot.DoesNotExist:
            snap = None
        if snap is None:
            try:
                snap = RealtimeSnapshot.objects.filter(symbol=symbol).first()
            except Exception:
                snap = None
        if snap is None:
            return None
        return {
            'price': float(snap.price),
            'change_pct': float(snap.change),
            'volume': int(snap.volume),
            'turnover': float(snap.turnover),
            'high': float(snap.high),
            'low': float(snap.low),
            'open_price': float(snap.open_price),
            'pre_close': float(snap.pre_close),
        }

    def _fundamentals(self, symbol):
        """基本面占位：后续可从数据源扩展。

        返回 ``None`` 表示暂无基本面数据，因子计算将安全降级。
        """
        return None

    def _resolve_symbol(self, symbol):
        if isinstance(symbol, Symbol):
            return symbol
        try:
            return Symbol.objects.filter(code=symbol).first()
        except Exception:
            return None

    def build(self, symbol, context=None, frequency='1d', count=50, end_time=None):
        """构建统一数据上下文。

        ``context`` 为可选注入的事件/配置 payload，会保留其原始键。
        """
        raw_symbol = symbol
        symbol_obj = self._resolve_symbol(symbol)
        market_data = []
        source = 'database'
        price = context.get('last_close') if context else None

        if self.prefer_db and symbol_obj is not None:
            market_data = _db_kline(symbol_obj, count=count, end_time=end_time)
            if market_data:
                latest = market_data[-1]
                if latest.get('close') is not None:
                    price = float(latest['close'])

        if not market_data and self.broker is not None:
            raw_code = symbol_obj.code if symbol_obj is not None else raw_symbol
            try:
                df = self.broker.history_n(
                    symbol=raw_code, frequency=frequency, count=count,
                    end_time=end_time, fields=None, data_frame=True,
                )
                records = df.to_dict('records') if df is not None and hasattr(df, 'to_dict') else []
                market_data = records
                if market_data:
                    source = 'sdk'
            except Exception:
                market_data = []

        snapshot = self._snapshot(symbol_obj) if symbol_obj is not None else None
        if price is None and snapshot:
            price = snapshot.get('price')
        last_close = None
        if market_data:
            close = market_data[-1].get('close')
            last_close = float(close) if close is not None else (float(price) if price is not None else None)
        elif price is not None:
            last_close = float(price)

        built = {
            'symbol': symbol_obj.code if symbol_obj is not None else str(raw_symbol),
            'frequency': frequency,
            'market_data': market_data,
            'snapshot': snapshot,
            'fundamentals': self._fundamentals(symbol_obj),
            'price': price,
            'last_close': last_close,
            'data_source': source,
            'asof': timezone.now().isoformat(),
        }
        if context:
            merged = dict(context)
            merged.update(built)
            merged['trigger_payload'] = {
                k: v for k, v in context.items() if k not in built
            }
            return merged
        return built


def build_data_context(symbol, context=None, broker=None, frequency='1d',
                       count=50, end_time=None):
    """便捷入口：一次性构建 Case 数据上下文。"""
    return DataContextBuilder(broker=broker).build(
        symbol, context=context, frequency=frequency, count=count, end_time=end_time,
    )