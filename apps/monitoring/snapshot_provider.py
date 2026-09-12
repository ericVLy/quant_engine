"""实时行情快照 Provider（模块9）。

akshare 的 spot 接口按「市场全量」返回 DataFrame（``stock_zh_a_spot_em`` 等），
因此 Provider 采用 ``fetch_market(market) -> {code: {field: value}}`` 的批量形态，
避免逐标的请求。与 ``runner/fundamentals.py`` 的依赖注入模式一致，测试注入 mock，
不依赖外部网络。

字段契约（缺失字段降级为 None）：
price / change(涨跌幅%) / volume(累计成交量) / amount(累计成交额)
/ high / low / open_price / pre_close
"""
import logging
from abc import ABC, abstractmethod

import akshare as ak
import pandas as pd

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


class AkshareSpotProvider(MarketSnapshotProvider):
    """基于 akshare spot 接口的实现（A/HK/US 三市场）。"""

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
        return normalize_spot_frame(df, market)