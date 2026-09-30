# pylint: disable=too-many-statements,too-many-locals  # 已知偏大：编排型主流程，拆分需专门重构（暂不处理）；已知偏大：编排型主流程需同时持有多个局部上下文，拆分需专门重构
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
import types

import pandas as pd
import akshare as akshare_lib

try:
    import ashare as ashare_lib
except ImportError:  # pragma: no cover
    from . import ashare as ashare_lib

from django.db import connections
from apps.watchlists.models import Symbol
from apps.watchlists.services import normalize_a_share_code, resolve_a_share_exchange
from .models import (
    KLineSyncLog,
    get_kline_database_alias, get_kline_table_name, ensure_kline_table
)

logger = logging.getLogger(__name__)


def get_kline_table_name_for_symbol(symbol):
    return get_kline_table_name(symbol)


def query_kline_table(symbol, start_date, end_date):
    """按 symbol + 日期范围查询对应分表中的 K 线记录。"""
    table_name = ensure_kline_table(symbol)
    db_alias = get_kline_database_alias()
    if symbol.market == 'A':
        select_sql = "SELECT date, open, high, low, close, volume, amount, adj_factor, turnover_rate FROM {} WHERE date BETWEEN %s AND %s ORDER BY date".format(table_name)
    elif symbol.market == 'HK':
        select_sql = "SELECT date, open, high, low, close, volume, amount, prev_close, currency FROM {} WHERE date BETWEEN %s AND %s ORDER BY date".format(table_name)
    elif symbol.market == 'US':
        select_sql = "SELECT date, open, high, low, close, volume, amount, split_factor, pre_market_price, after_hours_price FROM {} WHERE date BETWEEN %s AND %s ORDER BY date".format(table_name)
    else:
        raise ValueError(f"不支持的市场类型: {symbol.market}")

    with connections[db_alias].cursor() as cursor:
        cursor.execute(select_sql, [start_date, end_date])
        rows = cursor.fetchall()

    results = []
    for row in rows:
        if symbol.market == 'A':
            date_val, open_val, high_val, low_val, close_val, volume_val, amount_val, adj_factor, turnover_rate = row
            item = {
                'symbol': symbol.code,
                'date': date_val,
                'open': open_val,
                'high': high_val,
                'low': low_val,
                'close': close_val,
                'volume': volume_val,
                'amount': amount_val,
                'extra': {'adj_factor': adj_factor, 'turnover_rate': turnover_rate},
            }
        elif symbol.market == 'HK':
            date_val, open_val, high_val, low_val, close_val, volume_val, amount_val, prev_close, currency = row
            item = {
                'symbol': symbol.code,
                'date': date_val,
                'open': open_val,
                'high': high_val,
                'low': low_val,
                'close': close_val,
                'volume': volume_val,
                'amount': amount_val,
                'extra': {'prev_close': prev_close, 'currency': currency},
            }
        else:
            date_val, open_val, high_val, low_val, close_val, volume_val, amount_val, split_factor, pre_market_price, after_hours_price = row
            item = {
                'symbol': symbol.code,
                'date': date_val,
                'open': open_val,
                'high': high_val,
                'low': low_val,
                'close': close_val,
                'volume': volume_val,
                'amount': amount_val,
                'extra': {
                    'split_factor': split_factor,
                    'pre_market_price': pre_market_price,
                    'after_hours_price': after_hours_price,
                },
            }
        results.append(item)

    return results


def ashare_get_price(symbol, start_date, end_date, frequency='1d', count=None):
    """封装项目内的 ashare 模块，统一处理日期和数量参数。"""
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

    if count is None:
        count = max(1, (end_date - start_date).days + 1)

    if not hasattr(ashare_lib, 'get_price'):
        raise ValueError('ashare 模块未提供 get_price() 接口')

    return ashare_lib.get_price(
        symbol.code,
        end_date=end_date,
        count=count,
        frequency=frequency,
    )


def _coerce_series_or_scalar(value, default=0):
    if isinstance(value, pd.Series):
        return pd.to_numeric(value, errors='coerce').fillna(default)
    return pd.Series([pd.to_numeric(value, errors='coerce') if pd.notna(value) else default])


def _normalize_ashare_kline_dataframe(df, symbol):
    """将 ashare 返回的 DataFrame 规范成与 AkShare 兼容的字段结构。"""
    if df is None:
        return pd.DataFrame()
    if isinstance(df, list):
        return pd.DataFrame()
    if not hasattr(df, 'copy'):
        return pd.DataFrame()

    result = df.copy()
    if result.empty:
        return result

    rename_map = {
        '日期': 'date',
        '时间': 'date',
        'day': 'date',
        'time': 'date',
        '开盘': 'open',
        '收盘': 'close',
        '最高': 'high',
        '最低': 'low',
        '成交量': 'volume',
        '成交额': 'amount',
        '涨跌幅': 'change_pct',
        '涨跌额': 'change',
        '换手率': 'turnover_rate',
        '复权因子': 'adj_factor',
    }
    result = result.rename(columns={k: v for k, v in rename_map.items() if k in result.columns})

    if 'date' not in result.columns:
        if 'day' in result.columns:
            result = result.rename(columns={'day': 'date'})
        elif 'time' in result.columns:
            result = result.rename(columns={'time': 'date'})
        elif isinstance(result.index, pd.DatetimeIndex) or result.index.name not in (None, '') or not result.index.equals(pd.RangeIndex(start=0, stop=len(result), step=1)):
            result = result.reset_index()
            if 'index' in result.columns and 'date' not in result.columns:
                result = result.rename(columns={'index': 'date'})
            elif result.columns[0] not in {'date', 'open', 'high', 'low', 'close', 'volume'}:
                result = result.rename(columns={result.columns[0]: 'date'})
            elif result.index.name not in (None, '') and result.index.name not in result.columns:
                result = result.rename(columns={result.index.name: 'date'})
        else:
            result = result.reset_index().rename(columns={'index': 'date'})

    if 'date' in result.columns:
        result['date'] = pd.to_datetime(result['date'], errors='coerce').dt.date

    for col in ['open', 'high', 'low', 'close', 'volume']:
        if col in result.columns:
            result[col] = pd.to_numeric(result[col], errors='coerce').fillna(0)

    if 'amount' not in result.columns:
        if {'close', 'volume'}.issubset(result.columns):
            result['amount'] = pd.to_numeric(result['close'] * result['volume'], errors='coerce').fillna(0)
        else:
            result['amount'] = 0
    else:
        result['amount'] = pd.to_numeric(result['amount'], errors='coerce').fillna(0)

    if symbol.market == 'A':
        if 'adj_factor' not in result.columns:
            result['adj_factor'] = 1.0
        result['adj_factor'] = pd.to_numeric(result['adj_factor'], errors='coerce').fillna(1.0)
        if 'turnover_rate' not in result.columns:
            result['turnover_rate'] = 0
        result['turnover_rate'] = pd.to_numeric(result['turnover_rate'], errors='coerce').fillna(0)
        if 'change_pct' not in result.columns:
            result['change_pct'] = 0
        result['change_pct'] = pd.to_numeric(result['change_pct'], errors='coerce').fillna(0)
        if 'change' not in result.columns:
            result['change'] = 0
        result['change'] = pd.to_numeric(result['change'], errors='coerce').fillna(0)
    elif symbol.market == 'HK':
        if 'prev_close' not in result.columns:
            result['prev_close'] = 0
        result['prev_close'] = pd.to_numeric(result['prev_close'], errors='coerce').fillna(0)
        result['currency'] = result.get('currency', 'HKD')
    elif symbol.market == 'US':
        if 'split_factor' not in result.columns:
            result['split_factor'] = 1.0
        result['split_factor'] = pd.to_numeric(result['split_factor'], errors='coerce').fillna(1.0)
        result['pre_market_price'] = pd.to_numeric(result.get('pre_market_price', None), errors='coerce')
        result['after_hours_price'] = pd.to_numeric(result.get('after_hours_price', None), errors='coerce')

    result = result.sort_values('date').reset_index(drop=True)
    return result


def _stock_zh_a_hist_compat(symbol, period='daily', start_date=None, end_date=None, adjust='qfq'):
    """兼容旧 AkShare 接口：返回与原 `stock_zh_a_hist` 一致的中文列名。"""
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    if start_date is None:
        start_date = end_date - timedelta(days=30)
    if end_date is None:
        end_date = datetime.now().date()

    count = max(1, (end_date - start_date).days + 1)
    df = ashare_get_price(type('S', (), {'code': symbol, 'market': 'A'})(), start_date, end_date, frequency='1d', count=count)
    result = _normalize_ashare_kline_dataframe(df, type('S', (), {'market': 'A'})())
    if result.empty:
        return result
    return result.rename(columns={
        'date': '日期',
        'open': '开盘',
        'close': '收盘',
        'high': '最高',
        'low': '最低',
        'volume': '成交量',
        'amount': '成交额',
        'change_pct': '涨跌幅',
        'change': '涨跌额',
        'turnover_rate': '换手率',
        'adj_factor': '复权因子',
    })


class _CompatAshareModule(types.SimpleNamespace):
    def __init__(self):
        super().__init__()
        self.stock_zh_a_hist = _stock_zh_a_hist_compat


ak = _CompatAshareModule()


# 美股东财市场前缀：105=NASDAQ，106=NYSE，107=AMEX
_US_EXCHANGE_PREFIXES = ('105', '106', '107')


def _format_akshare_date(value):
    """把 date/datetime/str 统一转成 akshare 东财接口要求的 YYYYMMDD 字符串。"""
    if isinstance(value, str):
        value = datetime.strptime(value, '%Y-%m-%d').date()
    return value.strftime('%Y%m%d')


def _normalize_hk_symbol_code(code):
    """港股代码规整为东财接口要求的 5 位（'700' → '00700'，已带前缀的剥掉）。"""
    value = str(code or '').strip().upper()
    if value.startswith('HK$') or value.startswith('HK.'):
        value = value[3:]
    elif value.startswith('HK'):
        value = value[2:]
    digits = re.sub(r'[^0-9]', '', value)
    return digits.zfill(5) if digits else str(code or '').strip()


def fetch_kline_from_akshare_hk(symbol, start_date, end_date, adjust='qfq'):
    """港股日线：akshare ``stock_hk_hist``（东方财富）。

    返回与 A 股一致的规范化 DataFrame（date/open/high/low/close/volume/amount）。
    拉取失败抛 ``ValueError``（携带可定位原因），供同步日志与前端提示展示。
    """
    hk_code = _normalize_hk_symbol_code(symbol.code)
    try:
        df = akshare_lib.stock_hk_hist(
            symbol=hk_code,
            period='daily',
            start_date=_format_akshare_date(start_date),
            end_date=_format_akshare_date(end_date),
            adjust=adjust if adjust in ('qfq', 'hfq') else '',
        )
    except Exception as exc:
        raise ValueError(f"港股 {hk_code} 日线拉取失败（akshare stock_hk_hist）：{exc}") from exc
    if df is None or df.empty:
        raise ValueError(f"港股 {hk_code} 未查询到 {start_date}~{end_date} 的日线数据，请确认代码是否正确")
    return _normalize_ashare_kline_dataframe(df, symbol)


def fetch_kline_from_akshare_us(symbol, start_date, end_date, adjust='qfq'):
    """美股日线：akshare ``stock_us_hist``（东方财富，需交易所市场前缀）。

    代码已带前缀（如 ``105.AAPL``）时直接使用；纯代码（如 ``AAPL``）依次尝试
    NASDAQ/NYSE/AMEX。全部失败抛 ``ValueError``（携带可定位原因）。
    """
    us_code = str(symbol.code or '').strip().upper()
    if re.fullmatch(r'(105|106|107)\.[A-Z0-9.\-]+', us_code):
        candidates = [us_code]
    else:
        candidates = [f'{prefix}.{us_code}' for prefix in _US_EXCHANGE_PREFIXES]

    last_error = None
    for candidate in candidates:
        try:
            df = akshare_lib.stock_us_hist(
                symbol=candidate,
                period='daily',
                start_date=_format_akshare_date(start_date),
                end_date=_format_akshare_date(end_date),
                adjust=adjust if adjust in ('qfq', 'hfq') else '',
            )
        except Exception as exc:
            last_error = exc
            logger.warning(f"美股 {candidate} 日线拉取失败：{exc}")
            continue
        if df is not None and not df.empty:
            return _normalize_ashare_kline_dataframe(df, symbol)

    detail = f"；最后错误：{last_error}" if last_error else "（接口返回空数据）"
    raise ValueError(
        f"美股 {us_code} 未查询到 {start_date}~{end_date} 的日线数据"
        f"（已尝试 NASDAQ/NYSE/AMEX 市场前缀）{detail}"
    )


def _a_share_fetch_code(symbol):
    """A 股拉取用代码：修正 000xxx 二义段的指数语义。

    库中 exchange 显式标注沪市（SSE/SHSE/SH/XSHG）的 000 开头代码（如
    000300 沪深300）必须以 ``sh`` 前缀传给 ashare，否则会被保守判为深市
    个股而拉错数据（见 apps/watchlists.services 统一规则）。
    """
    raw = str(symbol.code or '').strip()
    normalized = normalize_a_share_code(raw)
    if (
        normalized.startswith('000')
        and resolve_a_share_exchange(raw, str(getattr(symbol, 'exchange', '') or '')) == 'SSE'
    ):
        return f'sh{normalized}'
    return symbol.code


def fetch_kline_from_ashare(symbol, start_date, end_date, adjust='qfq'):
    """按市场分派拉取日线数据，返回统一的规范化 DataFrame。

    - A 股：ashare 适配层（sina 主源 + 腾讯备用）；
    - 港股：akshare ``stock_hk_hist``（东方财富）；
    - 美股：akshare ``stock_us_hist``（东方财富）。

    旧函数名保留（历史调用兼容），docstring 以当前实现为准。
    """
    logger.info(f"Fetching {symbol.market} K线数据 for {symbol.code} from {start_date} to {end_date} via ashare")
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

    if symbol.market == 'HK':
        return fetch_kline_from_akshare_hk(symbol, start_date, end_date, adjust)
    if symbol.market == 'US':
        return fetch_kline_from_akshare_us(symbol, start_date, end_date, adjust)

    if symbol.market == 'A' and hasattr(ak, 'stock_zh_a_hist'):
        df = ak.stock_zh_a_hist(
            symbol=_a_share_fetch_code(symbol),
            start_date=start_date,
            end_date=end_date,
            period='daily',
            adjust=adjust,
        )
        normalized_df = _normalize_ashare_kline_dataframe(
            df.rename(columns={
                '日期': 'date',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
                '涨跌幅': 'change_pct',
                '涨跌额': 'change',
                '换手率': 'turnover_rate',
                '复权因子': 'adj_factor',
            }) if not df.empty else df,
            symbol,
        )
    else:
        count = max(1, (end_date - start_date).days + 1)
        df = ashare_get_price(symbol, start_date, end_date, frequency='1d', count=count)
        normalized_df = _normalize_ashare_kline_dataframe(df, symbol)

    if normalized_df.empty:
        logger.warning(f"No data returned for {symbol.code} from ashare")
    return normalized_df


def fetch_kline_from_akshare(symbol, start_date, end_date, adjust='qfq'):
    """兼容旧命名：保留 AkShare 调用入口，但实际走 ashare 实现。"""
    logger.warning("fetch_kline_from_akshare() 已弃用，切换为 ashare 实现")
    return fetch_kline_from_ashare(symbol, start_date, end_date, adjust)


def _fetch_existing_dates(symbol, table_name, db_alias, start_date, end_date):
    """查询区间内已入库的日期列表（升序）。仅用于同步去重/缺口判断（写入路径允许 symbol_id 过滤）。"""
    with connections[db_alias].cursor() as cursor:
        cursor.execute(
            f"SELECT date FROM {table_name} WHERE symbol_id = %s AND date BETWEEN %s AND %s ORDER BY date",
            [symbol.id, start_date, end_date],
        )
        return [row[0] for row in cursor.fetchall()]


def sync_kline_for_symbol(symbol, sync_type='daily', start_date=None, end_date=None, adjust='qfq'):
    """
    同步指定标的的 K 线数据
    返回: (records_added, records_skipped, error_msg)

    增量优化：拉取前先查区间内已入库日期——
    - 已覆盖整个请求区间（首尾都已有数据）→ 直接跳过远端拉取，省流量；
    - 区间头部已覆盖（最早一条 <= start_date）→ 拉取窗口收窄为 (最新一条, end_date]；
    - 区间头部未覆盖（可能有头部缺口）→ 保持全量拉取，由逐行去重兜底
      （显式传入早于缺口的 start_date 可补齐历史缺口）。
    """
    if sync_type != 'daily':
        raise ValueError("当前仅支持日线同步")

    if start_date is None:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=30)
    else:
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

    table_name = ensure_kline_table(symbol)
    db_alias = get_kline_database_alias()

    # ---- 增量判断：避免重复拉取已入库日期 ----
    existing_dates = _fetch_existing_dates(symbol, table_name, db_alias, start_date, end_date)
    if existing_dates:
        min_existing, max_existing = existing_dates[0], existing_dates[-1]
        if max_existing >= end_date and min_existing <= start_date:
            # 区间首尾均已有数据：视为已覆盖（非交易日不产生数据），跳过远端拉取
            logger.info(f"{symbol.code} {start_date}~{end_date} 已全部入库，跳过拉取")
            return 0, len(existing_dates), None
        if min_existing <= start_date and max_existing < end_date:
            # 头部已覆盖：只拉最新一条之后的缺口
            fetch_start = max_existing + timedelta(days=1)
            if fetch_start > end_date:
                return 0, len(existing_dates), None
            logger.info(f"{symbol.code} 增量拉取 {fetch_start}~{end_date}（已有至 {max_existing}）")
            start_date = fetch_start

    try:
        df = fetch_kline_from_ashare(symbol, start_date, end_date, adjust)
    except Exception as e:
        logger.error(f"从 ashare 获取 {symbol.code} 数据失败: {e}")
        return 0, 0, str(e)

    if df is None or df.empty:
        return 0, 0, "无数据返回"

    added = 0
    skipped = 0
    for _, row in df.iterrows():
        date_val = row['date']
        if isinstance(date_val, str):
            date_val = datetime.strptime(date_val, '%Y-%m-%d').date()
        elif isinstance(date_val, datetime):
            date_val = date_val.date()

        with connections[db_alias].cursor() as cursor:
            if connections[db_alias].vendor == 'sqlite':
                cursor.execute(
                    f"SELECT 1 FROM {table_name} WHERE symbol_id = %s AND date = %s LIMIT 1",
                    [symbol.id, date_val]
                )
            else:
                cursor.execute(
                    f"SELECT 1 FROM {table_name} WHERE symbol_id = %s AND date = %s LIMIT 1",
                    [symbol.id, date_val]
                )
            exists = cursor.fetchone() is not None
        if exists:
            skipped += 1
            continue
        columns = ['symbol_id', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'created_at', 'updated_at']
        values = [
            symbol.id,
            date_val,
            Decimal(str(row.get('open', 0))),
            Decimal(str(row.get('high', 0))),
            Decimal(str(row.get('low', 0))),
            Decimal(str(row.get('close', 0))),
            int(row.get('volume', 0)),
            Decimal(str(row.get('amount', 0))) if row.get('amount') else None,
            datetime.now(),
            datetime.now(),
        ]
        if symbol.market == 'A':
            columns += ['adj_factor', 'turnover_rate']
            values += [
                Decimal(str(row.get('adj_factor', 1.0))),
                Decimal(str(row.get('turnover_rate', 0))) if row.get('turnover_rate') else None,
            ]
        elif symbol.market == 'HK':
            columns += ['prev_close', 'currency']
            values += [None, 'HKD']
        elif symbol.market == 'US':
            columns += ['split_factor', 'pre_market_price', 'after_hours_price']
            values += [
                Decimal(str(row.get('split_factor', 1.0))),
                None,
                None,
            ]

        placeholders = ', '.join(['%s'] * len(values))
        columns_sql = ', '.join(columns)
        with connections[db_alias].cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table_name} ({columns_sql}) VALUES ({placeholders})",
                values,
            )

        added += 1

    return added, skipped, None


def sync_all_symbols(sync_type='daily', start_date=None, end_date=None, adjust='qfq'):
    """同步所有活跃标的的数据，按市场分别处理"""
    symbols = Symbol.objects.all()
    results = []
    for sym in symbols:
        added, skipped, error = sync_kline_for_symbol(sym, sync_type, start_date, end_date, adjust)
        # 记录同步日志（即使失败也记录）
        KLineSyncLog.objects.create(
            symbol=sym,
            sync_type=sync_type,
            start_date=start_date or (datetime.now() - timedelta(days=30)).date(),
            end_date=end_date or datetime.now().date(),
            records_added=added,
            records_skipped=skipped,
            status='success' if error is None else 'failed',
            error_msg=error or ''
        )
        results.append({
            'symbol': sym.code,
            'added': added,
            'skipped': skipped,
            'error': error
        })
    return results
