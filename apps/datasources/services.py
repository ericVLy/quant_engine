import logging
import json
from datetime import datetime, timedelta
from decimal import Decimal
import types

import pandas as pd
import akshare as ak

try:
    import ashare as ashare_lib
except ImportError:  # pragma: no cover
    from . import ashare as ashare_lib

from django.db import transaction, connections
from apps.watchlists.models import Symbol
from .models import (
    AStockKLine, HKStockKLine, USStockKLine, KLineSyncLog,
    get_kline_database_alias, get_kline_table_name, ensure_kline_table
)

logger = logging.getLogger(__name__)


def get_kline_model(symbol):
    """兼容接口：根据标的市场返回基类模型，实际数据以动态分表运行时模型存储。"""
    if symbol.market == 'A':
        return AStockKLine
    elif symbol.market == 'HK':
        return HKStockKLine
    elif symbol.market == 'US':
        return USStockKLine
    else:
        raise ValueError(f"不支持的市场类型: {symbol.market}")


def get_kline_table_name_for_symbol(symbol):
    return get_kline_table_name(symbol)


def query_kline_table(symbol, start_date, end_date):
    """按 symbol + 日期范围查询对应分表中的 K 线记录。"""
    table_name = ensure_kline_table(symbol)
    db_alias = get_kline_database_alias()
    if symbol.market == 'A':
        select_sql = "SELECT date, open, high, low, close, volume, amount, adj_factor, turnover_rate, symbol_id FROM {} WHERE symbol_id = %s AND date BETWEEN %s AND %s ORDER BY date".format(table_name)
    elif symbol.market == 'HK':
        select_sql = "SELECT date, open, high, low, close, volume, amount, prev_close, currency, symbol_id FROM {} WHERE symbol_id = %s AND date BETWEEN %s AND %s ORDER BY date".format(table_name)
    elif symbol.market == 'US':
        select_sql = "SELECT date, open, high, low, close, volume, amount, split_factor, pre_market_price, after_hours_price, symbol_id FROM {} WHERE symbol_id = %s AND date BETWEEN %s AND %s ORDER BY date".format(table_name)
    else:
        raise ValueError(f"不支持的市场类型: {symbol.market}")

    with connections[db_alias].cursor() as cursor:
        cursor.execute(select_sql, [symbol.id, start_date, end_date])
        rows = cursor.fetchall()

    results = []
    for row in rows:
        if symbol.market == 'A':
            date_val, open_val, high_val, low_val, close_val, volume_val, amount_val, adj_factor, turnover_rate, _ = row
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
            date_val, open_val, high_val, low_val, close_val, volume_val, amount_val, prev_close, currency, _ = row
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
            date_val, open_val, high_val, low_val, close_val, volume_val, amount_val, split_factor, pre_market_price, after_hours_price, _ = row
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

    if results:
        return results

    legacy_model = get_kline_model(symbol)
    legacy_qs = legacy_model.objects.filter(symbol=symbol, date__range=[start_date, end_date]).order_by('date')
    for item in legacy_qs:
        record = {
            'symbol': symbol.code,
            'date': item.date,
            'open': item.open,
            'high': item.high,
            'low': item.low,
            'close': item.close,
            'volume': item.volume,
            'amount': item.amount,
            'extra': {}
        }
        if symbol.market == 'A':
            record['extra']['adj_factor'] = getattr(item, 'adj_factor', None)
            record['extra']['turnover_rate'] = getattr(item, 'turnover_rate', None)
        elif symbol.market == 'HK':
            record['extra']['prev_close'] = getattr(item, 'prev_close', None)
            record['extra']['currency'] = getattr(item, 'currency', None)
        elif symbol.market == 'US':
            record['extra']['split_factor'] = getattr(item, 'split_factor', None)
            record['extra']['pre_market_price'] = getattr(item, 'pre_market_price', None)
            record['extra']['after_hours_price'] = getattr(item, 'after_hours_price', None)
        results.append(record)
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


def fetch_kline_from_ashare(symbol, start_date, end_date, adjust='qfq'):
    """使用 ashare 模块获取 K 线数据，并保持与旧 AkShare 返回结构一致。"""
    logger.info(f"Fetching {symbol.market} K线数据 for {symbol.code} from {start_date} to {end_date} via ashare")
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

    if symbol.market == 'A' and hasattr(ak, 'stock_zh_a_hist'):
        df = ak.stock_zh_a_hist(
            symbol=symbol.code,
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


def fetch_kline_from_akshare_old(symbol, start_date, end_date, adjust='qfq'):
    """
    使用 AkShare 获取 K 线数据
    返回 pandas DataFrame，字段包含: date, open, high, low, close, volume, amount, ...
    """
    # 确保日期为 date 对象
    logger.info(f"Fetching {symbol.market} K线数据 for {symbol.code} from {start_date} to {end_date}")
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

    if symbol.market == 'A':
        df = ak.stock_zh_a_hist(
            symbol=symbol.code,
            period='daily',
            start_date=start_date.strftime('%Y%m%d'),
            end_date=end_date.strftime('%Y%m%d'),
            adjust=adjust
        )
        if df.empty:
            logger.warning(f"No data returned for {symbol.code} from AkShare")
            return df
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
            '换手率': 'turnover_rate'
        }, inplace=True)
        return df
    elif symbol.market == 'HK':
        # 港股（示例，实际接口可能不同）
        df = ak.stock_hk_daily(symbol=symbol.code, adjust='qfq')
        if df.empty:
            return df
        df.rename(columns={
            'date': 'date',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'volume': 'volume',
            'turnover': 'amount'
        }, inplace=True)
        return df
    elif symbol.market == 'US':
        df = ak.stock_us_daily(symbol=symbol.code, adjust='qfq')
        if df.empty:
            return df
        df.rename(columns={
            'date': 'date',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'volume': 'volume'
        }, inplace=True)
        return df
    else:
        raise ValueError(f"不支持的市场: {symbol.market}")



def sync_kline_for_symbol(symbol, sync_type='daily', start_date=None, end_date=None, adjust='qfq'):
    """
    同步指定标的的 K 线数据
    返回: (records_added, records_skipped, error_msg)
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
    LegacyKLineModel = get_kline_model(symbol)
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
        # if LegacyKLineModel.objects.filter(symbol=symbol, date=date_val).exists():
        #     skipped += 1
        #     continue

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

        # legacy_data = {
        #     'symbol': symbol,
        #     'date': date_val,
        #     'open': Decimal(str(row.get('open', 0))),
        #     'high': Decimal(str(row.get('high', 0))),
        #     'low': Decimal(str(row.get('low', 0))),
        #     'close': Decimal(str(row.get('close', 0))),
        #     'volume': int(row.get('volume', 0)),
        #     'amount': Decimal(str(row.get('amount', 0))) if row.get('amount') else None,
        # }
        # if symbol.market == 'A':
        #     legacy_data['adj_factor'] = Decimal(str(row.get('adj_factor', 1.0)))
        #     legacy_data['turnover_rate'] = Decimal(str(row.get('turnover_rate', 0))) if row.get('turnover_rate') else None
        # elif symbol.market == 'HK':
        #     legacy_data['prev_close'] = None
        #     legacy_data['currency'] = 'HKD'
        # elif symbol.market == 'US':
        #     legacy_data['split_factor'] = Decimal(str(row.get('split_factor', 1.0)))
        #     legacy_data['pre_market_price'] = None
        #     legacy_data['after_hours_price'] = None

        # LegacyKLineModel.objects.create(**legacy_data)
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