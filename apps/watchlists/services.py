import logging
import akshare as ak
from .models import Symbol


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


def resolve_a_share_exchange(code, exchange=''):  # pylint: disable=too-many-return-statements  # 交易所/名称归一是多分支查表，扁平早返回比深嵌套更易读
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


logger = logging.getLogger(__name__)

#: 指数代码 → 中文名（离线兜底，不依赖网络；仅覆盖常见宽基）
INDEX_NAMES = {
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

#: 少量高频标的的离线兜底名（在线源与缓存都不可用时）
COMMON_NAMES = {
    '000001': '平安银行',
    '000002': '万科A',
    '600036': '招商银行',
    '601166': '兴业银行',
    'AAPL': 'Apple Inc.',
    'MSFT': 'Microsoft Corporation',
    '00700': '腾讯控股',
}

#: 各市场在线名称源的取数函数与结果列名（``akshare``）
_MARKET_SOURCES = {
    'A': ('stock_info_a_code_name', 'code', 'akshare_code_name'),
    'HK': ('stock_hk_spot', 'code', 'akshare_hk_spot'),
    'US': ('stock_us_spot', 'symbol', 'akshare_us_spot'),
}


def normalize_name_lookup_code(code):
    """把任意写法的标的代码归一为「缓存与在线表共用的键」。

    统一处理：``SHSE.600000`` / ``600000.SH`` / ``sh600000`` / ``600000``
    全部归一为 ``600000``；港股 ``00700``/``700``/``0700.HK`` 归一为 ``00700``；
    美股 ``AAPL``/``aapl.o`` 归一为大写 ``AAPL``。

    这是缓存能命中的前提——在线源返回的代码形态各异（AkShare 港股常给
    ``00700``、美股给 ``AAPL.O``），不归一就会缓存写进去也查不回来。
    """
    text = str(code or '').strip().upper()
    if not text:
        return ''
    # 交易所前缀段（SHSE.600000）
    if '.' in text:
        head, _, tail = text.partition('.')
        # 形如 AAPL.O：左边不是交易所而是代码本体
        if head in {'SHSE', 'SZSE', 'BJSE', 'HK', 'US', 'XSHG', 'XSHE'}:
            text = tail
        else:
            text = head
    # 数字类代码的交易所后缀（600000.SH）与sh/sz/bj 前缀
    for suffix in ('.SH', '.SZ', '.BJ', '.HK', '.SS'):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    for prefix in ('SH', 'SZ', 'BJ'):
        if text.startswith(prefix) and text[2:].isdigit():
            text = text[2:]
            break
    text = text.strip()
    # 纯数字：港股补足 5 位（700 → 00700，与 AkShare 港股代码一致）；A 股补足 6 位
    if text.isdigit():
        if len(text) <= 5:
            text = text.zfill(5)
        elif len(text) < 6:
            text = text.zfill(6)
    return text


def normalize_index_lookup_code(code):
    """保留 ``sh``/``sz`` 前缀的归一形式，专用于查 :data:`INDEX_NAMES`。

    与 :func:`normalize_name_lookup_code` 的区别是**不剥离 ``sh``/``sz`` 前缀**。
    这是刻意的：``000xxx`` 段天然二义——``000001`` 既是深市个股「平安银行」
    又是沪市指数「上证指数」。只有调用方**显式写了** ``sh000001`` 才表示指数，
    裸 ``000001`` 按深市个股处理（保守默认，见文件头规则）。

    因此**不能**用"给裸代码加 ``sh`` 前缀再猜"的方式查指数表，否则
    ``000001`` 会被误判成上证指数。
    """
    text = str(code or '').strip().lower()
    if not text:
        return ''
    for suffix in ('.sh', '.sz', '.bj', '.hk', '.ss'):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    # 交易所在点号前且后段是纯数字：SHSE.000001 → sh000001
    if '.' in text:
        head, _, tail = text.partition('.')
        if head in {'shse', 'szse', 'bjse', 'xshg', 'xshe'} and tail.isdigit():
            text = ('sh' if head in {'shse', 'xshg'} else 'sz') + tail
    # 注意：**不剥离** sh/sz 前缀（那正是区分指数/个股的语义），只补零对齐位数。
    if text.isdigit():
        text = text.zfill(6) if len(text) >= 5 else text.zfill(5)
    return text


def normalize_market_hint(market):
    """把市场提示归一为 ``A`` / ``HK`` / ``US``（容错 ``sh``/``SSE``/``szse`` 等写法）。

    此前直接把 ``market.upper()`` 与 ``'A'`` 比较，导致调用方传 ``sh``/``SSE``
    时三个分支全部落空、静默退化成硬编码兜底。
    """
    text = str(market or '').strip().upper()
    if not text:
        return ''
    if text in {'A', 'SH', 'SZ', 'BJ', 'SSE', 'SHSE', 'SZSE', 'BSE', 'BJSE', 'XSHG', 'XSHE'}:
        return 'A'
    if text in {'HK', 'HKEX', 'HKG'}:
        return 'HK'
    if text in {'US', 'USA', 'NASDAQ', 'NYSE', 'AMEX'}:
        return 'US'
    return text


def _cache_ttl_hours():
    """名称缓存有效期（小时）。名称极少变动，默认 24h，可用环境变量覆盖。"""
    import os

    from django.conf import settings as django_settings

    configured = getattr(django_settings, 'SYMBOL_NAME_CACHE_TTL_HOURS', None)
    if configured is not None:
        return int(configured)
    return int(os.getenv('SYMBOL_NAME_CACHE_TTL_HOURS', '24'))


def fetch_market_name_map(market):
    """一次性拉取**整个市场**的「归一代码 → 名称」字典。

    这是唯一的在线出口：整市场一次请求，供批量入库/查询复用，
    取代"每解析一个代码就拉一次全市场表"的旧实现。

    Args:
        market: ``A`` / ``HK`` / ``US``（已归一）。

    Returns:
        tuple[dict[str, str], str]: ``({归一代码: 名称}, 数据来源标识)``；
        不支持的市场返回 ``({}, '')``。
    """
    spec = _MARKET_SOURCES.get(market)
    if spec is None:
        return {}, ''
    func_name, code_column, source_tag = spec
    getter = getattr(ak, func_name, None)
    if getter is None:
        logger.warning('akshare 缺少取数函数 %s（market=%s）', func_name, market)
        return {}, ''
    frame = getter()
    if frame is None or getattr(frame, 'empty', True):
        return {}, source_tag

    name_column = 'name' if 'name' in getattr(frame, 'columns', []) else None
    if name_column is None:
        logger.warning('%s 返回结果缺少 name 列（实际列：%s）', func_name,
                       list(getattr(frame, 'columns', [])))
        return {}, source_tag

    name_map = {}
    for _, row in frame.iterrows():
        key = normalize_name_lookup_code(row.get(code_column))
        if not key:
            continue
        value = str(row.get(name_column) or '').strip()
        if value:
            name_map[key] = value
    return name_map, source_tag


def refresh_symbol_name_cache(market, name_map=None, source=''):
    """把「代码 → 名称」字典批量写入持久化缓存。

    Args:
        market: ``A`` / ``HK`` / ``US``。
        name_map: 待写入的 ``{归一代码: 名称}``；``None`` 表示现场从在线源拉取。
        source: 数据来源标识（写入 ``source`` 列便于追溯）。

    Returns:
        int: 实际写入的行数。
    """
    from django.utils import timezone

    from .models import SymbolNameCache

    if name_map is None:
        name_map, source = fetch_market_name_map(market)
    if not name_map:
        return 0
    now = timezone.now()
    rows = [
        SymbolNameCache(market=market, code=code, name=name,
                        source=source or f'{market}:online', fetched_at=now)
        for code, name in name_map.items()
    ]
    # bulk_create + ignore_conflicts：唯一键冲突时更新为最新值。
    # 用 update_conflicts 让一次 SQL 批量 upsert，避免逐行 save() 的 N 次写入。
    SymbolNameCache.objects.bulk_create(
        rows, batch_size=1000, update_conflicts=True,
        update_fields=['name', 'source', 'fetched_at'],
        unique_fields=['market', 'code'],
    )
    logger.info('已刷新 %s 市场名称缓存：%d 条（来源 %s）', market, len(rows), source)
    return len(rows)


def _is_stale(fetched_at, ttl_hours=None):
    """该行的缓存是否已过期（``fetched_at`` 为``None`` 视为无数据）。"""
    from django.utils import timezone

    if fetched_at is None:
        return True
    ttl = _cache_ttl_hours() if ttl_hours is None else ttl_hours
    if ttl <= 0:
        return False
    return (timezone.now() - fetched_at).total_seconds() > ttl * 3600


def _market_needs_refresh(market):
    """该市场的名称缓存是否缺失或已过期（需要整市场重拉）。"""
    from .models import SymbolNameCache

    latest = (SymbolNameCache.objects.filter(market=market)
              .order_by('-fetched_at')
              .values_list('fetched_at', flat=True)
              .first())
    return _is_stale(latest)


def _lookup_cached_name(market, key):
    """查持久化缓存中的名称（命中返回名称，未命中返回 ``None``）。"""
    from .models import SymbolNameCache

    return (SymbolNameCache.objects
            .filter(market=market, code=key)
            .values_list('name', flat=True)
            .first())


def _lookup_cached_row(market, key):
    """查缓存行，返回 ``(name, fetched_at)``；未命中返回 ``None``。"""
    from .models import SymbolNameCache

    return (SymbolNameCache.objects
            .filter(market=market, code=key)
            .values_list('name', 'fetched_at')
            .first())


def resolve_symbol_name(code, market=None, refresh=None):  # pylint: disable=too-many-return-statements  # 解析链路是分层兜底（本地→指数表→缓存→刷新→过期兜底→内置名→代码本身），扁平早返回比深嵌套更易读
    """按代码解析中文名称：本地 → 持久化缓存 → 整市场刷新 → 离线兜底。

    相比旧实现的关键改进：

    1. **不再每查一个代码就拉一次全市场在线表**——命中 ``Symbol`` 表或
       ``SymbolNameCache`` 都不产生网络请求；只有缓存缺失/过期才整市场拉一次；
    2. **在线失败可离线降级**：回退到过期缓存（stale-while-error），
       再退到内置常见名，最后才返回代码本身；
    3. **代码归一统一**：跨市场（``SHSE.600000`` / ``700`` / ``aapl.o``）都能命中；
    4. **市场提示容错**：``sh``/``SSE``/``szse`` 等写法不再静默落空。

    Args:
        code: 标的代码（任意书写形态）。
        market: 市场提示 ``A``/``HK``/``US``；留空则按代码推断。
        refresh: 强制刷新缓存（``True``）；``None``（默认）按 TTL 自动判断。

    Returns:
        str: 名称；无法解析时返回归一后的代码本身（保持既有行为）。
    """
    code_str = str(code or '').strip()
    if not code_str:
        return ''

    market_hint = normalize_market_hint(market)
    resolved_market = market_hint or infer_market_from_code(code_str)
    normalized_code = normalize_name_lookup_code(code_str)
    if not normalized_code:
        return ''

    # 1) 本项目已纳管的标的：直接命中，不触发任何网络
    local = Symbol.objects.filter(code=normalized_code).exclude(name='').first()
    if local is not None:
        return local.name

    # 2) 指数离线表（常见宽基，避免为了一个指数去拉全市场）
    #    只认「显式带sh/sz 前缀」的写法：000001 归一后是裸数字，按深市个股处理，
    #    绝不能猜成 sh000001 → 上证指数（000xxx 段天然二义）。
    index_key = normalize_index_lookup_code(code_str)
    if index_key in INDEX_NAMES:
        return INDEX_NAMES[index_key]

    # 3) 持久化缓存。**过期行也要继续往下走刷新分支**，不能直接返回——
    #    否则 TTL 形同虚设、名称永远停在首次写入的值。过期值仅作刷新失败时的兜底。
    cached_row = _lookup_cached_row(resolved_market, normalized_code)
    stale = _is_stale(cached_row[1]) if cached_row else True

    # 4) 缓存缺失或过期 → 整市场拉一次并落库（批量解析时只会发生一次）
    should_refresh = refresh if refresh is not None else stale
    if should_refresh:
        try:
            name_map, source = fetch_market_name_map(resolved_market)
            if name_map:
                refresh_symbol_name_cache(resolved_market, name_map, source)
                cached = _lookup_cached_name(resolved_market, normalized_code)
                if cached:
                    return cached
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning('刷新 %s 名称缓存失败（继续用离线兜底）：%s', resolved_market, exc)

    # 5) 刷新未产出新值时，用过期缓存兜底（stale-while-error）
    if cached_row:
        return cached_row[0]

    # 6) 离线兜底：常见名 → 代码本身
    if normalized_code in COMMON_NAMES:
        return COMMON_NAMES[normalized_code]
    return normalized_code


def resolve_symbol_scope(symbol_scope):
    """
    解析 symbol_scope 配置，返回 Symbol 的 QuerySet

    支持三种格式：
    - {'type': 'all'}  → 返回所有标的
    - {'type': 'groups', 'group_ids': [1,2,3]} → 返回指定分组下的所有标的
    - {'type': 'symbols', 'symbol_codes': ['000001', '600036']} → 返回指定代码的标的

    该配置现在由 **Case** 持有（``Case.params['symbol_scope']``），
    Plan 不再直接声明标的范围。
    """
    if not symbol_scope:
        return Symbol.objects.none()

    scope_type = symbol_scope.get('type')
    if scope_type == 'all':
        return Symbol.objects.all()
    if scope_type == 'groups':
        group_ids = symbol_scope.get('group_ids', [])
        if not group_ids:
            return Symbol.objects.none()
        return Symbol.objects.filter(groups__id__in=group_ids).distinct()
    if scope_type == 'symbols':
        codes = symbol_scope.get('symbol_codes', [])
        if not codes:
            return Symbol.objects.none()
        return Symbol.objects.filter(code__in=codes)
    raise ValueError(f"不支持的 symbol_scope 类型: {scope_type}")


def case_symbol_scope(case):
    """取出一个 Case 声明的 ``symbol_scope``（缺省视为未声明）。

    Args:
        case: ``cases.models.Case`` 实例。

    Returns:
        dict | None: ``{'type': 'all'|'groups'|'symbols', ...}``；未声明时返回 ``None``。
    """
    scope = (case.params or {}).get('symbol_scope')
    return scope if isinstance(scope, dict) and scope else None


def case_declares_symbols(case) -> bool:
    """Case 是否声明了标的范围。"""
    return case_symbol_scope(case) is not None


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
        # （直接用本模块函数，勿再 from .services import —— 那是同模块自导入）
        code = normalize_a_share_code(code)

        # 交易所解析：exchange 字段优先；否则按代码段推断（指数与个股同规则）
        exchange = resolve_a_share_exchange(code, str(row.get('exchange') or '')) or 'SSE'

        market = 'A'

        try:
            _, created_flag = Symbol.objects.update_or_create(
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
