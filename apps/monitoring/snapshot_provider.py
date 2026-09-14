"""实时行情快照 Provider（模块9）。

采用 ``fetch_market(market, symbols) -> {code: {field: value}}`` 的批量形态。
两套实现 + 一个编排器：

- ``AkshareSpotProvider``：akshare 的 spot 接口按「市场全量」返回 DataFrame
  （``stock_zh_a_spot_em`` 等），H 港/US 美股全量拉取；
- ``GmSnapshotProvider``：gm SDK（掘金量化）按标的返回 tick 聚合快照，仅覆盖
  A 股（``SHSE.``/``SZSE.``）；
- ``CompositeSnapshotProvider``：默认编排——A 股优先 gm，HK/US 或 gm 失败时
  回退 akshare，保持多市场能力与韧性。

与 ``runner/fundamentals.py`` 的依赖注入模式一致，测试注入 mock，不依赖外部网络。

字段契约（缺失字段降级为 None）：
price / change(涨跌幅%) / volume(累计成交量) / amount(累计成交额)
/ high / low / open_price / pre_close
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from .market_calendar import MARKET_TIMEZONES, to_market_local

logger = logging.getLogger(__name__)

# 各市场 spot 接口返回列名别名（防御不同 akshare 版本的中英文列名差异）
_COLUMN_ALIASES = {
    'code': ['代码', '股票代码', 'Code', 'code', '代號'],
    'name': ['名称', '股票简称', '名稱', 'name'],
    'price': ['最新价', '现价', '最新', '現價', 'price', '最新價格'],
    'change': ['涨跌幅', '涨跌百分比', '百分比', 'change', '變動百分比', '涨跌'],
    'volume': ['成交量', '成交量(手)', '成交量(股)', 'stock_volume', 'volume', '成交量'],
    'amount': ['成交金额', '成交额', '成交金額', 'amount', '成交金额(港元)'],
    'high': ['最高', '日内最高', 'high', '最高價'],
    'low': ['最低', '日内最低', 'low', '最低價'],
    'open_price': ['开盘', '开盘价', '開盤', 'open'],
    'pre_close': ['昨收', '昨收价', '昨收盘价', '昨收價', 'pre_close'],
}

_MARKET_AK_FUNCS = {
    'A': 'stock_zh_a_spot_em',
    'HK': 'stock_hk_spot_em',
    'US': 'stock_us_spot_em',
}


def _pick_column(df, aliases):
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


def _to_number(value, default=None):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_returned_code(code):
    """统一返回代码：去交易所后缀、去空白；NaN/None 视为空；纯数字补足六位。"""
    try:
        if pd.isna(code):
            return ''
    except (TypeError, ValueError):
        pass
    code = str(code or '').strip()
    # 新浪指数接口的代码带交易所前缀（sh000300 / sz399001），剥前缀取 6 位数字
    if len(code) >= 8 and code[:2].lower() in ('sh', 'sz', 'bj') and code[2:].isdigit():
        code = code[2:]
    for suffix in ('.XSHG', '.XSHE', '.SZSE', '.HK', '.SH', '.SZ', '.BJ', '.US'):
        if code.upper().endswith(suffix):
            code = code[: -len(suffix)]
            break
    code = code.strip()
    if code.isdigit():
        return code.zfill(6)
    return code


def normalize_spot_frame(df, market):
    """把 akshare spot DataFrame 规范成 ``{code: {field: value}}``。

    - 缺少代码列时整份数据跳过；
    - 单字段缺列 / 缺值时降级为 None（数值字段调用方自行决定默认值）；
    - 单位差异不做换算，随上游数据源原样存储。
    """
    if df is None or not hasattr(df, 'columns') or len(df) == 0:
        return {}

    columns = {key: _pick_column(df, aliases) for key, aliases in _COLUMN_ALIASES.items()}
    code_col = columns.get('code')
    if code_col is None:
        logger.warning('[%s] spot 结果缺少代码列，跳过整份数据', str(market).upper())
        return {}

    out = {}
    for _, row in df.iterrows():
        code = _normalize_returned_code(row.get(code_col))
        if not code:
            continue
        item = {}
        for key, alias in columns.items():
            if key == 'code':
                continue
            item[key] = _to_number(row.get(alias)) if alias else None
        out[code] = item
    return out


class MarketSnapshotProvider(ABC):
    """实时行情快照抽象基类（依赖注入可替换实现）。"""

    @abstractmethod
    def fetch_market(self, market, symbols=None):
        """返回某市场全部标的快照 ``{code: {field: value}}``；失败抛异常由上层降级。"""

    def fetch_intraday_history(self, market, symbol, start, end):
        """返回 ``[start, end]``（aware UTC）内该标的逐分钟历史 bar（原始值，不做换算）。

        返回元素 dict（缺失字段为 None）：
        ``ts``(aware UTC，分钟对齐) / ``open`` / ``high`` / ``low`` / ``close``(→价格)
        / ``volume`` / ``amount`` / ``pre_close``(当日昨收，用于计算 change)。

        不支持逐分钟历史的 Provider 抛 ``NotImplementedError``，由上层回填逻辑静默跳过。
        """
        raise NotImplementedError('该数据源不支持逐分钟历史回填')


class AkshareSpotProvider(MarketSnapshotProvider):
    """基于 akshare spot 接口的实现（A/HK/US 三市场）。

    **指数与个股区分**：``stock_zh_a_spot_em`` 等 spot 接口只含个股；
    A 市场请求的标的中含指数（``is_a_share_index`` 判定）时，额外拉取
    ``stock_zh_index_spot_sina`` 指数全量行情并合并（仅合并想要的指数代码），
    保证 gm 不可用时指数仍可经 akshare 回退采样。
    """

    # 新浪指数 spot 接口（全市场指数：沪深 000xxx / 深证 399xxx 等）
    A_INDEX_FUNC = 'stock_zh_index_spot_sina'

    def __init__(self, ak_module=None):
        self._ak = ak_module or ak

    def fetch_market(self, market, symbols=None):
        market = str(market).upper()
        func_name = _MARKET_AK_FUNCS.get(market)
        if func_name is None:
            raise ValueError(f'不支持的市场: {market}')
        func = getattr(self._ak, func_name, None)
        if func is None or not callable(func):
            raise RuntimeError(f'akshare 未提供接口 {func_name}')
        df = func()
        if df is None:
            return {}
        out = normalize_spot_frame(df, market)
        if market == 'A':
            out.update(self._fetch_index_spot(symbols or []))
        return out

    def _fetch_index_spot(self, symbols):
        """按需拉取指数 spot，仅返回请求清单中的指数代码。"""
        from apps.watchlists.services import is_a_share_index, normalize_a_share_code

        wanted = {
            normalize_a_share_code(getattr(s, 'code', ''))
            for s in symbols
            if is_a_share_index(getattr(s, 'code', ''), getattr(s, 'exchange', ''))
        }
        if not wanted:
            return {}
        func = getattr(self._ak, self.A_INDEX_FUNC, None)
        if func is None or not callable(func):
            logger.warning('akshare 未提供接口 %s，指数回退不可用', self.A_INDEX_FUNC)
            return {}
        try:
            df = func()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning('指数 spot 拉取失败: %s', exc)
            return {}
        index_frame = normalize_spot_frame(df, 'A')
        return {code: item for code, item in index_frame.items() if code in wanted}
def gm_symbol_for(symbol):
    """把本地 ``Symbol`` 转为 gm SDK 的 symbol（交易所代码.标的代码，A 股）。

    - gm SDK 仅覆盖国内市场；A 股交易所优先取 ``symbol.exchange``（SSE/SZSE/BSE），
      缺失时按代码段推断（6 开头等属上海，0/3 开头个股按深圳）；
    - **指数与个股区分**：399xxx 等指数专属段判为指数（SZSE.399001 深证成指等）；
      000xxx 二义段以 ``exchange`` 为准（SSE → SHSE.000001 上证指数；
      缺失/深市 → SZSE.000001 平安银行个股），避免混淆；
    - 港股/美股不在 gm 覆盖范围，直接抛「不支持」，由上层回退到其他数据源。
    """
    market = str(getattr(symbol, 'market', '') or '').upper()
    if market != 'A':
        raise ValueError(f'gm SDK 不支持的市场: {market}')
    code = str(getattr(symbol, 'code', '') or '').strip()
    if not code:
        raise ValueError('缺少标的代码，无法构造 gm symbol')
    from apps.watchlists.services import normalize_a_share_code, resolve_a_share_exchange
    # 传入原始代码：sh/sz/bj 前缀是显式市场标记（sh000001=上证指数），
    # 不能先剥前缀再解析交易所，否则 000xxx 二义段会误判为深市个股
    exchange = resolve_a_share_exchange(code, getattr(symbol, 'exchange', ''))
    code = normalize_a_share_code(code)
    prefix = {'SSE': 'SHSE', 'SZSE': 'SZSE', 'BSE': 'BJSE'}.get(exchange)
    if prefix is None:
        raise ValueError(f'无法解析标的交易所: code={code} exchange={getattr(symbol, "exchange", "")}')
    return f'{prefix}.{code}'


def _gm_time_str(dt, market):
    """把 aware UTC datetime 转为 market 本地时间字符串（gm 接口历史查询参数）。"""
    try:
        return to_market_local(dt, market).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def _parse_bar_ts(value, market):
    """把 gm bar 的时间（datetime / str）统一解析为 aware UTC 分钟时间；失败返回 None。

    - naive datetime / 字符串按 market 本地时区解释，再转 UTC（对齐 IntradayPoint.ts）；
    - aware datetime 直接转 UTC；微秒清 0。
    """
    market = str(market).upper()
    tz = ZoneInfo(MARKET_TIMEZONES[market])
    if value is None:
        return None
    if hasattr(value, 'tzinfo'):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(ZoneInfo('UTC')).replace(second=0, microsecond=0)
    try:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text)  # gm bob/eob：ISO 带时区字符串
        except ValueError:
            dt = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return None
    dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo('UTC')).replace(second=0, microsecond=0)


def _bar_time_value(bar):
    """提取 gm 行的原始时间字段：tick 用 ``time``，bar 用 ``bob``（起始时间）优先。"""
    for key in ('time', 'bar_time', 'bob', 'eob'):
        value = bar.get(key)
        if value is not None:
            return value
    return None


class GmSnapshotProvider(MarketSnapshotProvider):
    """基于 gm SDK 的实时快照实现（A 股 SHSE/SZSE）。

    与 akshare 的「按市场整份 DataFrame」不同，gm SDK 按标的返回行情，因此
    ``fetch_market`` 需要调用方传入该市场的标列表（``symbols``）。

    数据取自 gm ``history``（frequency='tick'，交易日内聚合快照，天然含累计值）：
    - price     ← tick.price（最新价）
    - open/high/low ← tick 当日统计
    - volume    ← tick.cum_volume（累计成交量）
    - amount    ← tick.cum_amount（累计成交额）
    - pre_close ← 前一日 1d bar 的 close
    - change    ← (price - pre_close) / pre_close * 100

    港股/美股不在 gm 覆盖范围，``fetch_market`` 对非 A 市场抛 ``ValueError``，
    由上层 ``CompositeSnapshotProvider`` 回退到其他数据源。
    """

    SUPPORTED_MARKETS = ('A',)
    FREQUENCY = 'tick'

    def __init__(self, broker=None):
        # broker 可注入（GmBrokerAdapter 或 mock）；惰性导入 gm 相关运行模块，
        # 保证无终端/无 token 的环境中仅导入本模块不报错。
        self.broker = broker

    def _get_broker(self):
        if self.broker is None:
            from runner.gm_adapter import GmBrokerAdapter
            self.broker = GmBrokerAdapter()
        return self.broker

    def fetch_market(self, market, symbols=None):
        market = str(market).upper()
        if market != 'A':
            raise ValueError(f'gm SDK 不支持的市场: {market}')
        out = {}
        for symbol in symbols or []:
            if str(getattr(symbol, 'market', '') or '').upper() != 'A':
                continue
            try:
                snap = self._snapshot(symbol)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning('[%s] gm 快照拉取失败: %s', getattr(symbol, 'code', symbol), exc)
                continue
            if snap is not None:
                out[getattr(symbol, 'code')] = snap
        return out

    def _snapshot(self, symbol):
        gm_symbol = gm_symbol_for(symbol)
        ticks = self._today_ticks(gm_symbol)
        if ticks:
            tick = ticks[-1]
            price = _to_number(tick.get('price'))
            volume = tick.get('cum_volume')
            amount = tick.get('cum_amount')
            high = tick.get('high')
            low = tick.get('low')
            open_price = tick.get('open')
        else:
            # tick 为空（部分环境/收盘后）时回退到当日 60s bar 聚合出快照语义
            day_bars = self._today_bars(gm_symbol)
            if not day_bars:
                return None
            price = _to_number(day_bars[-1].get('close'))
            volume = sum(_to_number(b.get('volume'), 0) or 0 for b in day_bars)
            amount = sum(_to_number(b.get('amount'), 0) or 0 for b in day_bars)
            highs = [_to_number(b.get('high')) for b in day_bars if _to_number(b.get('high')) is not None]
            lows = [_to_number(b.get('low')) for b in day_bars if _to_number(b.get('low')) is not None]
            high = max(highs) if highs else None
            low = min(lows) if lows else None
            open_price = _to_number(day_bars[0].get('open'))
        pre_close = self._pre_close(gm_symbol)
        change = None
        if price is not None and pre_close:
            change = round((price - pre_close) / pre_close * 100.0, 4)
        return {
            'price': price,
            'change': change,
            'volume': volume,
            'amount': amount,
            'high': high,
            'low': low,
            'open_price': open_price,
            'pre_close': pre_close,
        }

    def _today_ticks(self, gm_symbol):
        from django.utils import timezone
        from .market_calendar import to_market_local

        broker = self._get_broker()
        local = to_market_local(timezone.now(), 'A')
        raw = broker.history(
            symbol=gm_symbol, frequency=self.FREQUENCY,
            start_time=local.strftime('%Y-%m-%d 00:00:00'),
            end_time=local.strftime('%Y-%m-%d %H:%M:%S'),
        )
        return self._rows(raw)

    def _today_bars(self, gm_symbol):
        """当日 60s bar（用于 tick 为空时的快照回退聚合）。"""
        from django.utils import timezone
        from .market_calendar import to_market_local

        broker = self._get_broker()
        local = to_market_local(timezone.now(), 'A')
        raw = broker.history(
            symbol=gm_symbol, frequency='60s',
            start_time=local.strftime('%Y-%m-%d 09:30:00'),
            end_time=local.strftime('%Y-%m-%d %H:%M:%S'),
        )
        return self._rows(raw)

    def _pre_close(self, gm_symbol):
        """昨收：取**日期严格早于今日（市场本地）**的最近一根日 bar 的 close。

        不能按 ``bars[-2]`` 位置猜：gm ``history_n(frequency='1d')`` 在盘前/
        刚开盘（当日 bar 未生成）或非交易日时不含今日 bar，``bars[-2]``
        会变成前天收盘（bug：昨收错用前天价）。
        """
        try:
            raw = self._get_broker().history_n(
                symbol=gm_symbol, frequency='1d', count=5, data_frame=False,
            )
        except Exception:  # pylint: disable=broad-except
            return None
        bars = self._rows(raw)
        if not bars:
            return None
        from django.utils import timezone
        from .market_calendar import to_market_local

        today = to_market_local(timezone.now(), 'A').date()
        # 倒序找第一根日期早于今日的 bar（含 eob/bob/date/time 等字段兼容）
        for bar in reversed(bars):
            bar_date = self._bar_date(bar)
            if bar_date is None:
                continue
            if bar_date < today:
                return _to_number(bar.get('close'))
        # 全部无法解析日期时保守取最后一根（通常为最近交易日）
        return _to_number(bars[-1].get('close'))

    @staticmethod
    def _bar_date(bar):
        """从日 bar 提取交易日期（本地）；无法解析返回 None。"""
        for key in ('eob', 'bob', 'date', 'time', 'day'):
            value = bar.get(key)
            if value is None:
                continue
            if hasattr(value, 'date'):
                return value.date()
            try:
                return datetime.fromisoformat(str(value)).date()
            except ValueError:
                continue
            except TypeError:
                continue
        return None

    def fetch_intraday_history(self, market, symbol, start, end):
        """用 gm ``history(frequency='60s')`` 拉该标的 ``[start, end]``（aware UTC）逐分钟 bar。

        返回统一 bar dict（含当日 ``pre_close``，供回填计算 change）；
        非 A 市场抛 ``ValueError``，由 Composite 回退。
        """
        market = str(market).upper()
        if market != 'A':
            raise ValueError(f'gm SDK 不支持的市场: {market}')
        gm_symbol = gm_symbol_for(symbol)
        pre_close = self._pre_close(gm_symbol)
        broker = self._get_broker()
        raw = broker.history(
            symbol=gm_symbol, frequency='60s',
            start_time=_gm_time_str(start, market),
            end_time=_gm_time_str(end, market),
        )
        bars = self._rows(raw)
        out = []
        for bar in bars:
            ts = _parse_bar_ts(_bar_time_value(bar), market)
            if ts is None:
                continue
            amount = bar.get('amount')
            if amount is None:
                amount = bar.get('trade_amount')
            out.append({
                'ts': ts,
                'open': _to_number(bar.get('open')),
                'high': _to_number(bar.get('high')),
                'low': _to_number(bar.get('low')),
                'close': _to_number(bar.get('close')),
                'volume': _to_number(bar.get('volume')),
                'amount': _to_number(amount),
                'pre_close': pre_close,
            })
        return out

    @staticmethod
    def _rows(raw):
        """把 gm 返回的 list[dict] / DataFrame / 对象列表统一成 list[dict]。"""
        if raw is None:
            return []
        to_dict = getattr(raw, 'to_dict', None)
        if callable(to_dict):
            if getattr(raw, 'empty', False):
                return []
            return to_dict('records')
        try:
            return list(raw)
        except TypeError:
            return []
class CompositeSnapshotProvider(MarketSnapshotProvider):
    """多源实时快照：按 market 依次尝试 providers，任一成功即采用。

    - 默认主源为 gm SDK（A 股），作为「分时数据源」的替换；
    - gm 不覆盖的市场（HK/US）或拉取失败时，回退到 akshare，保持多市场能力与韧性。
    """

    def __init__(self, providers=None):
        self.providers = list(providers) if providers else [
            GmSnapshotProvider(),
            AkshareSpotProvider(),
        ]

    def fetch_market(self, market, symbols=None):
        errors = []
        for provider in self.providers:
            try:
                quotes = provider.fetch_market(market, symbols)
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(f'{provider.__class__.__name__}: {exc}')
                continue
            if quotes:
                return quotes
        if errors:
            raise RuntimeError('; '.join(errors))
        return {}

    def fetch_intraday_history(self, market, symbol, start, end):
        """逐源尝试逐分钟历史，首个非空即用；全部失败返回 []（上层回填静默跳过）。"""
        for provider in self.providers:
            try:
                bars = provider.fetch_intraday_history(market, symbol, start, end)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(
                    '[%s] 回填历史源 %s 失败: %s',
                    market, provider.__class__.__name__, exc,
                )
                continue
            if bars:
                return bars
            logger.info(
                '[%s] 回填历史源 %s 返回空数据（start=%s end=%s）',
                market, provider.__class__.__name__, start, end,
            )
        return []