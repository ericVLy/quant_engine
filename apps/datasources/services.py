import logging
import json
import akshare as ak
from datetime import datetime, timedelta
from decimal import Decimal
from django.db import transaction, connection
from apps.watchlists.models import Symbol
from .models import (
    AStockKLine, HKStockKLine, USStockKLine, KLineSyncLog,
    get_kline_table_name, ensure_kline_table
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
    if symbol.market == 'A':
        select_sql = "SELECT date, open, high, low, close, volume, amount, adj_factor, turnover_rate, symbol_id FROM {} WHERE symbol_id = %s AND date BETWEEN %s AND %s ORDER BY date".format(table_name)
    elif symbol.market == 'HK':
        select_sql = "SELECT date, open, high, low, close, volume, amount, prev_close, currency, symbol_id FROM {} WHERE symbol_id = %s AND date BETWEEN %s AND %s ORDER BY date".format(table_name)
    elif symbol.market == 'US':
        select_sql = "SELECT date, open, high, low, close, volume, amount, split_factor, pre_market_price, after_hours_price, symbol_id FROM {} WHERE symbol_id = %s AND date BETWEEN %s AND %s ORDER BY date".format(table_name)
    else:
        raise ValueError(f"不支持的市场类型: {symbol.market}")

    with connection.cursor() as cursor:
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


def fetch_kline_from_akshare(symbol, start_date, end_date, adjust='qfq'):
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
        logger.info(f"Fetching A股 K线数据 for {symbol.code} from {start_date} to {end_date} with adjust={adjust}")
        kwargs = {
            'symbol': symbol.code,
            'period': 'daily',
            'start_date': start_date.strftime('%Y%m%d'),
            'end_date': end_date.strftime('%Y%m%d'),
            'adjust': adjust}
        logger.info(f"Calling ak.stock_zh_a_hist with args: {json.dumps(kwargs, indent=4)}")
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
    LegacyKLineModel = get_kline_model(symbol)
    try:
        df = fetch_kline_from_akshare(symbol, start_date, end_date, adjust)
    except Exception as e:
        logger.error(f"从 AkShare 获取 {symbol.code} 数据失败: {e}")
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

        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT 1 FROM {table_name} WHERE symbol_id = %s AND date = %s LIMIT 1",
                [symbol.id, date_val]
            )
            exists = cursor.fetchone() is not None
        if exists:
            skipped += 1
            continue
        if LegacyKLineModel.objects.filter(symbol=symbol, date=date_val).exists():
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
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table_name} ({columns_sql}) VALUES ({placeholders})",
                values,
            )

        legacy_data = {
            'symbol': symbol,
            'date': date_val,
            'open': Decimal(str(row.get('open', 0))),
            'high': Decimal(str(row.get('high', 0))),
            'low': Decimal(str(row.get('low', 0))),
            'close': Decimal(str(row.get('close', 0))),
            'volume': int(row.get('volume', 0)),
            'amount': Decimal(str(row.get('amount', 0))) if row.get('amount') else None,
        }
        if symbol.market == 'A':
            legacy_data['adj_factor'] = Decimal(str(row.get('adj_factor', 1.0)))
            legacy_data['turnover_rate'] = Decimal(str(row.get('turnover_rate', 0))) if row.get('turnover_rate') else None
        elif symbol.market == 'HK':
            legacy_data['prev_close'] = None
            legacy_data['currency'] = 'HKD'
        elif symbol.market == 'US':
            legacy_data['split_factor'] = Decimal(str(row.get('split_factor', 1.0)))
            legacy_data['pre_market_price'] = None
            legacy_data['after_hours_price'] = None

        LegacyKLineModel.objects.create(**legacy_data)
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