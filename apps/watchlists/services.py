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


# ---------------------------------------------------------------------------
# A 股指数 / 个股判别（标的代码字符串处理的统一规则）
#
# 注意 000xxx 段天然二义：000001 既是深市个股「平安银行」也是沪市指数「上证指数」。
# 仅凭 6 位数字前缀无法区分，必须以 exchange 显式标注（SSE=沪指数）为准；
# 无 exchange 时按深市个股处理（保守默认，与历史数据一致）。
# ---------------------------------------------------------------------------

# 与个股代码不重叠的指数段：可纯前缀判定
# - 399xxx：深市指数（399001 深证成指 / 399006 创业板指等）
# - 880xxx：申万指数；930xxx/931xxx/932xxx/980xxx：中证系列指数；899xxx：北证指数
A_SHARE_INDEX_ONLY_PREFIXES = ('399', '880', '930', '931', '932', '980', '899')
# 沪市交易所别名（exchange 字段各种历史写法）
SSE_EXCHANGES = ('SSE', 'SHSE', 'SH', 'XSHG', 'XSHE_SH')
SZSE_EXCHANGES = ('SZSE', 'SZ', 'XSHE')


def normalize_a_share_code(code):
    """剥离交易所前/后缀与空白，返回 6 位数字代码；无法归一化返回原串。"""
    value = str(code or '').strip().upper()
    for prefix in ('SH', 'SZ', 'BJ'):
        if value.startswith(prefix) and value[2:].isdigit():
            value = value[2:]
            break
    for suffix in ('.XSHG', '.XSHE', '.SH', '.SZ', '.BJ'):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.zfill(6) if value.isdigit() else value


def is_a_share_index(code, exchange=''):
    """判断 6 位 A 股代码是否为指数（区别于个股）。

    - 指数专属段（399/880/930/931/932/980/899 开头）直接判为指数；
    - 000xxx 二义段：代码带显式沪市标记（``sh`` 前缀 / ``.SH`` 后缀）或
      ``exchange`` 为沪市（SSE/SHSE/SH）时判为指数；无标记或深市判为个股；
    - 非 6 位数字（美股/港股代码）恒为 False。
    """
    raw = str(code or '').strip().upper()
    normalized = normalize_a_share_code(raw)
    if not normalized.isdigit() or len(normalized) != 6:
        return False
    explicit_sse = (
        raw.startswith(('SH', 'XSHG')) or raw.endswith(('.SH', '.XSHG'))
        or str(exchange or '').strip().upper() in ('SSE', 'SHSE', 'SH', 'XSHG')
    )
    if normalized.startswith(A_SHARE_INDEX_ONLY_PREFIXES):
        return True
    if normalized.startswith('000'):
        return explicit_sse
    return False


def resolve_a_share_exchange(code, exchange=''):
    """解析 6 位 A 股代码的交易所（SSE/SZSE/BSE），指数与个股同规则。

    - ``exchange`` 显式给出且可识别时直接归一化采用（最高优先级）；
    - 沪市：6 开头个股，以及指数（000xxx 沪指数 / 880 / 930 / 931 / 932 / 980）；
    - 深市：0 / 2 / 3 开头个股与 399xxx 指数；
    - 北交所：4 / 8 开头（899xxx 北证指数除外，归沪证指规则见上，实际北证指
      数经 gm 用 BSE 前缀，此处统一返回 BSE）。
    """
    explicit = str(exchange or '').strip().upper()
    if explicit in ('SSE', 'SHSE', 'SH', 'XSHG'):
        return 'SSE'
    if explicit in ('SZSE', 'SZ', 'XSHE'):
        return 'SZSE'
    if explicit in ('BSE', 'BJ'):
        return 'BSE'
    raw = str(code or '').strip().upper()
    # 原始代码带交易所前缀（如 sh000001 上证指数）时，前缀即显式市场标记，
    # 优先于后续按 6 位数字段的推断——否则 sh000001 会被剥成 000001 而误判为深市个股
    if len(raw) >= 8 and raw[:2] in ('SH', 'SZ', 'BJ') and raw[2:].isdigit():
        return {'SH': 'SSE', 'SZ': 'SZSE', 'BJ': 'BSE'}[raw[:2]]
    normalized = normalize_a_share_code(code)
    if not normalized.isdigit() or len(normalized) != 6:
        return ''
    if normalized.startswith('6') or normalized.startswith('880'):
        return 'SSE'
    if normalized.startswith(('4', '8')) and not normalized.startswith('880'):
        return 'BSE'
    if normalized.startswith('930') or normalized.startswith('931') \
            or normalized.startswith('932') or normalized.startswith('980'):
        return 'SSE'
    if normalized.startswith('899'):
        return 'BSE'
    return 'SZSE'


def resolve_symbol_name(code, market=None):
    """通过代码和市场类型解析对应名称，失败时返回安全回退值。"""
    code_str = str(code or '').strip()
    if not code_str:
        return ''

    resolved_market = (market or infer_market_from_code(code_str)).upper()
    normalized_code = code_str.replace('.SH', '').replace('.SZ', '').replace('.BJ', '').replace('.HK', '')

    try:
        if resolved_market == 'A':
            index_code = {
                        'sh000001': '上证指数',
                        'sz399001': '深证成指',
                        'sz399006': '创业板指',
                        'sh000300': '沪深300',
                        'sh000905': '中证500',
                        'sh000852': '中证1000',
                        'sz399005': '中小板指',
                        'sz399102': '科创板指',
                        'sh000016': '上证50',
                        'sh000010': '上证180',
                        'sh000688': '科创50',
                        'sh000906': '中证800',
                        }
            if normalized_code in index_code:
                return index_code[normalized_code]
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

        # 去除可能的市场后缀，并归一化为 6 位数字
        from .services import normalize_a_share_code, resolve_a_share_exchange
        code = normalize_a_share_code(code)

        # 交易所解析：exchange 字段优先；否则按代码段推断（指数与个股同规则）
        exchange = resolve_a_share_exchange(code, str(row.get('exchange') or '')) or 'SSE'

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