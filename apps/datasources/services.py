import logging
import json
import akshare as ak
from datetime import datetime, timedelta
from decimal import Decimal
from django.db import transaction
from apps.watchlists.models import Symbol
from .models import AStockKLine, HKStockKLine, USStockKLine, KLineSyncLog

logger = logging.getLogger(__name__)


def get_kline_model(symbol):
    """根据标的的市场返回对应的 K 线模型类"""
    if symbol.market == 'A':
        return AStockKLine
    elif symbol.market == 'HK':
        return HKStockKLine
    elif symbol.market == 'US':
        return USStockKLine
    else:
        raise ValueError(f"不支持的市场类型: {symbol.market}")


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


@transaction.atomic
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

    KLineModel = get_kline_model(symbol)
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

        # 检查是否已存在
        exists = KLineModel.objects.filter(symbol=symbol, date=date_val).exists()
        if exists:
            skipped += 1
            continue

        data = {
            'symbol': symbol,
            'date': date_val,
            'open': Decimal(str(row.get('open', 0))),
            'high': Decimal(str(row.get('high', 0))),
            'low': Decimal(str(row.get('low', 0))),
            'close': Decimal(str(row.get('close', 0))),
            'volume': int(row.get('volume', 0)),
            'amount': Decimal(str(row.get('amount', 0))) if row.get('amount') else None,
        }
        # 市场特定字段
        if symbol.market == 'A':
            data['adj_factor'] = Decimal(1.0)
            data['turnover_rate'] = Decimal(str(row.get('turnover_rate', 0))) if row.get('turnover_rate') else None
        elif symbol.market == 'HK':
            data['prev_close'] = None
            data['currency'] = 'HKD'
        elif symbol.market == 'US':
            data['split_factor'] = Decimal(1.0)
            data['pre_market_price'] = None
            data['after_hours_price'] = None

        KLineModel.objects.create(**data)
        added += 1

    return added, skipped, None


@transaction.atomic
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