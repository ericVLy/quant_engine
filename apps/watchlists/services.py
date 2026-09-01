import logging
import akshare as ak
from django.core.exceptions import ValidationError
from .models import Symbol, Group


def infer_market_from_code(code):
    """根据代码前缀推断市场类型。"""
    code_str = str(code or '').strip()
    if not code_str:
        return 'A'
    if code_str.startswith(('600', '601', '603', '605', '000', '001', '002', '003', '004', '300')):
        return 'A'
    if code_str.startswith(('7', '8', '9')):
        return 'HK'
    return 'US' if code_str.isdigit() else 'A'


def resolve_symbol_name(code, market=None):
    """通过代码和市场类型解析对应名称，失败时返回安全回退值。"""
    code_str = str(code or '').strip()
    if not code_str:
        return ''

    resolved_market = (market or infer_market_from_code(code_str)).upper()
    normalized_code = code_str.replace('.SH', '').replace('.SZ', '').replace('.BJ', '').replace('.HK', '')

    try:
        if resolved_market == 'A':
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty:
                df = df.copy()
                df['code'] = df['code'].astype(str).str.replace('.SH', '').str.replace('.SZ', '').str.replace('.BJ', '')
                match = df[df['code'] == normalized_code]
                if not match.empty:
                    return str(match.iloc[0].get('name', ''))
        elif resolved_market == 'HK':
            df = ak.stock_hk_spot()
            if df is not None and not df.empty:
                df = df.copy()
                df['code'] = df['code'].astype(str)
                match = df[df['code'] == normalized_code]
                if not match.empty:
                    return str(match.iloc[0].get('name', ''))
        elif resolved_market == 'US':
            df = ak.stock_us_spot()
            if df is not None and not df.empty:
                df = df.copy()
                df['symbol'] = df['symbol'].astype(str)
                match = df[df['symbol'] == normalized_code]
                if not match.empty:
                    return str(match.iloc[0].get('name', ''))
    except Exception as exc:
        logger.warning(f"resolve_symbol_name 失败，code={normalized_code}, market={resolved_market}, error={exc}")

    common_names = {
        '000001': '平安银行',
        '000002': '万科A',
        '600036': '招商银行',
        '601166': '兴业银行',
        'AAPL': 'Apple Inc.',
        'MSFT': 'Microsoft Corporation',
        '00700': '腾讯控股',
    }
    if normalized_code in common_names:
        return common_names[normalized_code]

    return normalized_code

logger = logging.getLogger(__name__)


def resolve_symbol_scope(symbol_scope):
    """
    解析 Plan 的 symbol_scope 配置，返回 Symbol 的 QuerySet

    支持三种格式：
    - {'type': 'all'}  → 返回所有标的
    - {'type': 'groups', 'group_ids': [1,2,3]} → 返回指定分组下的所有标的
    - {'type': 'symbols', 'symbol_codes': ['000001', '600036']} → 返回指定代码的标的
    """
    if not symbol_scope:
        return Symbol.objects.none()

    scope_type = symbol_scope.get('type')
    if scope_type == 'all':
        return Symbol.objects.all()
    elif scope_type == 'groups':
        group_ids = symbol_scope.get('group_ids', [])
        if not group_ids:
            return Symbol.objects.none()
        return Symbol.objects.filter(groups__id__in=group_ids).distinct()
    elif scope_type == 'symbols':
        codes = symbol_scope.get('symbol_codes', [])
        if not codes:
            return Symbol.objects.none()
        return Symbol.objects.filter(code__in=codes)
    else:
        raise ValueError(f"不支持的 symbol_scope 类型: {scope_type}")


def sync_market_data():
    """
    从 AkShare 同步全市场 A 股标的到 Symbol 表
    返回: {'created': 新增数, 'updated': 更新数}
    """
    df = ak.stock_info_a_code_name()
    created = 0
    updated = 0

    for _, row in df.iterrows():
        code = row['code']
        name = row['name']

        # 去除可能的市场后缀
        code = code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')

        # 根据代码前缀判断交易所
        if code.startswith('6'):
            exchange = 'SSE'
        elif code.startswith(('0', '3')):
            exchange = 'SZSE'
        elif code.startswith('8'):
            exchange = 'BSE'
        else:
            exchange = 'SSE'

        market = 'A'

        try:
            obj, created_flag = Symbol.objects.update_or_create(
                code=code,
                defaults={
                    'name': name,
                    'exchange': exchange,
                    'market': market
                }
            )
            if created_flag:
                created += 1
            else:
                updated += 1
        except Exception as e:
            logger.error(f"同步 {code} 失败: {e}")
            continue

    logger.info(f"同步完成: 新增 {created} 条，更新 {updated} 条")
    return {'created': created, 'updated': updated}