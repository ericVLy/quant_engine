# pylint: disable=bad-indentation,multiple-statements,dangerous-default-value,inconsistent-return-statements,invalid-name,redefined-outer-name,multiple-imports,wrong-import-order,use-maxsplit-arg,unnecessary-semicolon,missing-timeout  # 本文件为第三方 get_price 片段原样移植（services.ashare_get_price 依赖它），风格不符本仓规范，重排风险高于收益
#-*- coding:utf-8 -*-    --------------Ashare 股票行情数据双核心版( https://github.com/mpquant/Ashare )
import json,requests,datetime;      import pandas as pd  #

# 港股：腾讯 hkfqkline 的代码前缀 + 5 位补零（'700' → '00700'；实测 'hk700' 返回空数组）
_HK_TX_PREFIX = 'hk'
# 美股：腾讯 usfqkline 的代码前缀 + 交易所后缀（.OQ=NASDAQ / .N=NYSE / .A=AMEX）
_US_TX_PREFIX = 'us'
_US_TX_EXCHANGE_SUFFIX = {'.OQ': 'NASDAQ', '.N': 'NYSE', '.A': 'AMEX'}
# 腾讯返回行数上限（实测 count=1024 可用、2000 返回空），超过会被静默截断为空
_TX_MAX_COUNT = 1024
# 美股「交易所后缀回退」的最低覆盖度（返回根数 / 请求根数）：
# ETF 类标的（AGQ / SCO 等）在 NASDAQ 通道会返回非空但仅当天 1 根，
# 必须以覆盖度而非「非空」判定命中，否则永远取不到 NYSE Arca 的历史数据。
_US_MIN_COVERAGE_RATIO = 0.5
# 新浪美股通道单次返回上限（实测单次即返回全部历史，count 只是安全上限）
_SINA_MAX_COUNT = 8000

def _normalize_ashare_code(code):
    """统一 A 股代码格式，确保腾讯/Sina 接口接受带交易所前缀的代码。

    **指数与个股区分**（见 ``apps/watchlists.services`` 的统一规则）：
    - 399xxx 等指数专属段 → 深市指数（``sz399001``）；
    - 000xxx 二义段：调用方传入显式 ``sh``/``sh000300`` 前缀时保留沪市指数语义；
      裸 6 位数字保守按深市个股处理（与 ``gm_symbol_for`` 的缺省一致）；
    - 880/930/931/932/980 指数段 → 沪市指数（``sh930xxx``）。
    """
    code = str(code or '').strip()
    if not code:
        return code
    explicit_market = ''
    upper = code.upper()
    if upper.startswith(('SH', 'SZ')):
        explicit_market = upper[:2].lower()
        code = code[2:]
    code = code.replace('.XSHG', '').replace('.XSHE', '').replace('.SH', '').replace('.SZ', '')
    if code.isdigit() and len(code) < 6:
        code = code.zfill(6)
    if code.startswith(('600', '601', '603', '605', '688', '689', '880', '930', '931', '932', '980')):
        return f'sh{code}'
    if code.startswith(('000', '001', '002', '003', '004', '300', '301', '399')):
        return f'{explicit_market or "sz"}{code}'
    if code.startswith('bj'):
        return code
    return code


# ============ 港股 / 美股代码规整（腾讯通道） ============
# 注意：_normalize_ashare_code 只适用于 A 股——它对非 6 位数字会原样返回，
# 但会把 '00700' 当深市代码、给美股字母代码加 'sh' 前缀，因此港美股必须走独立规整。

def _normalize_hk_code_tx(code):
    """港股代码规整为腾讯 hk 通道要求：``hk`` + 5 位数字。

    - 剥掉 ``HK$`` / ``HK.`` / ``hk`` 前缀与 ``.HK`` 后缀；
    - 纯数字补零到 5 位（``700`` → ``hk00700``；实测 ``hk700`` 返回空数组，必须补零）；
    - 非纯数字（如带字母的港股代码）原样加前缀交由腾讯判定。
    """
    value = str(code or '').strip().upper()
    if value.startswith('HK$') or value.startswith('HK.'):
        value = value[3:]
    elif value.startswith('HK'):
        value = value[2:]
    value = value.replace('.HK', '')
    digits = ''.join(ch for ch in value if ch.isdigit())
    if digits and len(digits) != len(value.replace('.', '').replace('-', '')):
        # 含非数字成分（如 '00700.HK' 已处理完、'R_00700' 之类），不做补零改造
        return _HK_TX_PREFIX + value
    return _HK_TX_PREFIX + (digits.zfill(5) if digits else value)


def _us_exchange_candidates(code):
    """美股代码的交易所后缀候选（腾讯 us 通道按后缀区分交易所）。

    库中代码可能已带后缀（``AAPL.OQ``）或纯代码（``AAPL``）；
    纯代码按 NASDAQ → NYSE → AMEX 顺序尝试（与东财 105/106/107 的回退思路一致）。
    """
    value = str(code or '').strip().upper()
    if value.startswith('US'):
        value = value[2:]
    for suffix in _US_TX_EXCHANGE_SUFFIX:
        if value.endswith(suffix):
            return [value]
    return [f'{value}{suffix}' for suffix in _US_TX_EXCHANGE_SUFFIX]


#---腾讯日线---  2025-12-21日正常使用
def get_price_day_tx(code, end_date='', count=10, frequency='1d'):     #日线获取
    unit='week' if frequency in '1w' else 'month' if frequency in '1M' else 'day'     #判断日线，周线，月线
    code = _normalize_ashare_code(code)
    if end_date:
        end_date = end_date.strftime('%Y-%m-%d') if isinstance(end_date, datetime.date) else str(end_date).split(' ')[0]
    end_date='' if end_date==datetime.datetime.now().strftime('%Y-%m-%d') else end_date   #如果日期今天就变成空
    URL=f'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{unit},,{end_date},{count},qfq'
    try:
        response = requests.get(URL, timeout=20)
        response.raise_for_status()
        st = response.json() if hasattr(response, 'json') else json.loads(response.content)
    except Exception:
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    data = st.get('data') if isinstance(st, dict) else None
    if not data or not isinstance(data, dict):
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    stk = data.get(code)
    if not stk or not isinstance(stk, dict):
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    ms='qfq'+unit
    buf = stk.get(ms) if ms in stk else stk.get(unit)
    if not buf:
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    df=pd.DataFrame(buf,columns=['time','open','close','high','low','volume'],dtype='float')
    df.time=pd.to_datetime(df.time);    df.set_index(['time'], inplace=True);   df.index.name=''          #处理索引
    return df

#腾讯分钟线
def get_price_min_tx(code, end_date=None, count=10, frequency='1d'):    #分钟线获取
    ts=int(frequency[:-1]) if frequency[:-1].isdigit() else 1           #解析K线周期数
    if end_date: end_date=end_date.strftime('%Y-%m-%d') if isinstance(end_date,datetime.date) else end_date.split(' ')[0]
    URL=f'http://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m{ts},,{count}'
    st= json.loads(requests.get(URL).content);       buf=st['data'][code]['m'+str(ts)]
    df=pd.DataFrame(buf,columns=['time','open','close','high','low','volume','n1','n2'])
    df=df[['time','open','close','high','low','volume']]
    df[['open','close','high','low','volume']]=df[['open','close','high','low','volume']].astype('float')
    df.time=pd.to_datetime(df.time);   df.set_index(['time'], inplace=True);   df.index.name=''          #处理索引
    df['close'][-1]=float(st['data'][code]['qt'][code][3])                #最新基金数据是3位的
    return df


#sina新浪全周期获取函数，分钟线 5m,15m,30m,60m  日线1d=240m   周线1w=1200m  1月=7200m
def get_price_sina(code, end_date='', count=10, frequency='60m'):    #新浪全周期获取函数
    frequency=frequency.replace('1d','240m').replace('1w','1200m').replace('1M','7200m');   mcount=count
    ts=int(frequency[:-1]) if frequency[:-1].isdigit() else 1       #解析K线周期数
    if (end_date!='') & (frequency in ['240m','1200m','7200m']):
        end_date = pd.to_datetime(end_date).to_pydatetime() if not isinstance(end_date, datetime.datetime) else end_date
        unit=4 if frequency=='1200m' else 29 if frequency=='7200m' else 1    #4,29多几个数据不影响速度
        count=count + (datetime.datetime.now() - end_date).days // unit            #结束时间到今天有多少天自然日(肯定 >交易日)
        #print(code,end_date,count)
    URL=f'http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale={ts}&ma=5&datalen={count}'
    try:
        response = requests.get(URL, timeout=20)
        dstr = response.json() if hasattr(response, 'json') else json.loads(response.content)
    except Exception:
        return pd.DataFrame(columns=['day','open','high','low','close','volume'])
    if not dstr:
        return pd.DataFrame(columns=['day','open','high','low','close','volume'])
    df= pd.DataFrame(dstr,columns=['day','open','high','low','close','volume'])
    if df.empty:
        return df
    df['open'] = df['open'].astype(float); df['high'] = df['high'].astype(float);                          #转换数据类型
    df['low'] = df['low'].astype(float);   df['close'] = df['close'].astype(float);  df['volume'] = df['volume'].astype(float)
    df.day=pd.to_datetime(df.day);    df.set_index(['day'], inplace=True);     df.index.name=''            #处理索引
    if (end_date!='') & (frequency in ['240m','1200m','7200m']):
        return df[df.index<=end_date][-mcount:]
    return df


#---腾讯港股/美股日线---  与 A 股共用 fqkline 风格，但行字段数不同，单独解析
def _tx_kline_unit(unit):
    """把 frequency 映射为腾讯 unit 参数。"""
    return 'week' if unit in '1w' else 'month' if unit in '1M' else 'day'


def _tx_end_date_arg(end_date):
    """腾讯的 end 参数：当日传空串（当日 bar 尚未收盘时接口返回不完整数据）。"""
    if not end_date:
        return ''
    value = end_date.strftime('%Y-%m-%d') if isinstance(end_date, datetime.date) else str(end_date).split(' ')[0]
    return '' if value == datetime.datetime.now().strftime('%Y-%m-%d') else value


def _fetch_tx_market_daily(symbol, path, end_date='', unit='day', count=10, fq='qfq', amount_unit=1.0):
    """腾讯港美股日线公共拉取。返回 DataFrame，字段 time/open/close/high/low/volume/amount。

    - ``start`` 参数腾讯通道会忽略（总是返回截至 end 的最近 count 根），故不传；
    - 无数据时接口仍返回 HTTP 200 + 空数组，这里统一返回空 DataFrame，由调用方判定。

    ``amount_unit``：成交额换算系数——**腾讯港股返回万元、美股返回美元（系数 1）**，
    两者量纲不同，混用会把港股成交额缩小 1e4 倍（实测 PDD 等部分美股也会带 amount 字段）。
    """
    empty = pd.DataFrame(columns=['time', 'open', 'close', 'high', 'low', 'volume', 'amount'])
    count = max(1, min(int(count), _TX_MAX_COUNT))
    URL = f'https://web.ifzq.gtimg.cn/appstock/app/{path}/get?param={symbol},{unit},,{_tx_end_date_arg(end_date)},{count},{fq}'
    try:
        response = requests.get(URL, timeout=20)
        response.raise_for_status()
        st = response.json() if hasattr(response, 'json') else json.loads(response.content)
    except Exception:
        return empty
    data = st.get('data') if isinstance(st, dict) else None
    stk = (data or {}).get(symbol) if isinstance(data, dict) else None
    if not stk or not isinstance(stk, dict):
        return empty
    buf = stk.get(fq + unit) or stk.get(unit)
    if not buf:
        return empty

    rows = []
    for item in buf:
        if len(item) < 6:
            continue
        # 行字段数**不固定**：港股 9 列（含成交额，万元）、多数美股 6 列（无成交额）、
        # 部分美股 11 列（含成交额，美元）。故按列数 + 市场量纲双重判断，不能只看长度。
        raw_amount = item[8] if len(item) >= 9 else None
        try:
            amount = float(raw_amount) * amount_unit if raw_amount not in (None, '') else None
        except (TypeError, ValueError):
            amount = None
        rows.append([item[0], item[1], item[2], item[3], item[4], item[5], amount])

    df = pd.DataFrame(rows, columns=['time', 'open', 'close', 'high', 'low', 'volume', 'amount'])
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['time'])
    df.time = pd.to_datetime(df.time, errors='coerce')
    df = df.dropna(subset=['time']).set_index(['time'])
    df.index.name = ''
    df = df.sort_index()
    # 全列无成交额时**丢弃 amount 列**（而非留 NaN）：
    # 上游 _normalize_ashare_kline_dataframe 仅在「无 amount 列」时才用 close*volume 兜底，
    # 留 NaN 会被 fillna(0) 归零 → 美股成交额全部丢失（实测 AAPL/BABA 为 6 列无成交额行）。
    if 'amount' in df.columns and df['amount'].isna().all():
        df = df.drop(columns=['amount'])
    return df


def _fetch_sina_symbol(code):
    """归一化新浪美股接口所需的纯代码。

    库内代码可能带 ``us`` 前缀（腾讯通道格式）或交易所后缀（``.OQ``/``.N``/``.A``），
    新浪接口只认纯代码，故统一剥离并转大写。
    """
    value = str(code or '').strip().upper()
    if value.startswith('US'):
        value = value[2:]
    return value.split('.')[0].strip()


def _sina_end_bound(end_date):
    """把 end_date 归一化为 ``YYYY-MM-DD`` 字符串，用于过滤「晚于 end 的行」。

    空值表示不设上界（取到最新）。接受 ``date`` / ``datetime`` / 字符串。
    """
    if not end_date:
        return ''
    if isinstance(end_date, datetime.datetime):
        return end_date.strftime('%Y-%m-%d')
    if isinstance(end_date, datetime.date):
        return end_date.strftime('%Y-%m-%d')
    return str(end_date).strip()[:10]


def get_price_day_sina_us(code, end_date='', count=10, frequency='1d', adjust='qfq'):
    """美股日线（新浪 ``US_MinKService.getDailyK`` 通道）。

    **存在的意义**：腾讯与东财对部分标的（实测 ProShares 反向杠杆 ETF ``AGQ``/``SCO``）
    **都没有历史数据**——腾讯仅返回当天 1 根且 ``qfqday``/``hfqday`` 恒为空，
    指定更早的 ``end`` 直接返回 0 根。新浪则提供完整日线序列
    （实测 AGQ 自 2008-12-04 起 4483 根，SCO 自 2008-11-25 起 4489 根）。

    接口返回 **JSON 数组**，字段为单字母缩写，需转换为本模块统一格式
    （``time`` 为 DatetimeIndex，其余为 open/close/high/low/volume/amount）：

    ==============  =========  =========================================
    新浪字段        内部字段    说明
    ==============  =========  =========================================
    ``d``           time       交易日
    ``o``           open       开盘
    ``h``           high       最高
    ``l``           low        最低
    ``c``           close      收盘
    ``v``           volume     成交量（股）
    ``a``           amount     成交额（**美元**，与腾讯美股量纲一致，无需换算）
    ==============  =========  =========================================

    - 成交额若缺失/为 0，丢弃 ``amount`` 列交由上游 ``close*volume`` 兜底
      （同 ``_fetch_tx_market_daily`` 的处理，避免整列 NaN 被归零）；
    - 新浪**只提供不复权数据**，无 qfq/hfq 之分，故 ``adjust`` 参数被忽略——
      对反向杠杆 ETF 而言复权本身也无意义（每日按倍数重置）；
    - 接口对无效代码返回 HTTP 200 + 空数组，由调用方判定失败。
    """
    empty = pd.DataFrame(columns=['time', 'open', 'close', 'high', 'low', 'volume', 'amount'])
    symbol = _fetch_sina_symbol(code)
    if not symbol:
        return empty

    URL = (
        'https://stock.finance.sina.com.cn/usstock/api/json_v2.php/'
        f'US_MinKService.getDailyK?symbol={symbol}'
    )
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'}
    try:
        response = requests.get(URL, timeout=20, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return empty
    if not isinstance(payload, list) or not payload:
        return empty

    # 腾讯按「最近 count 根」返回；**新浪不支持 end/count 参数，一次性返回全部历史**，
    # 因此必须先按 end_date 截断「不晚于 end 的部分」，再取最近 count 根——
    # 否则对历史窗口（如 2019-2021）会截到最新而非窗口末尾，导致取不到任何数据。
    limit = max(1, min(int(count), _SINA_MAX_COUNT)) if count else None
    end_bound = _sina_end_bound(end_date)
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        day = str(item.get('d') or '').strip()[:10]
        if not day:
            continue
        if end_bound and day > end_bound:
            continue
        rows.append([day, item.get('o'), item.get('c'), item.get('h'),
                     item.get('l'), item.get('v'), item.get('a')])
    if limit:
        rows = rows[-limit:]

    df = pd.DataFrame(rows, columns=['time', 'open', 'close', 'high', 'low', 'volume', 'amount'])
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['time'])
    df.time = pd.to_datetime(df.time, errors='coerce')
    df = df.dropna(subset=['time']).set_index(['time'])
    df.index.name = ''
    df = df.sort_index()
    if 'amount' in df.columns and df['amount'].isna().all():
        df = df.drop(columns=['amount'])
    return df


def get_price_day_tx_hk(code, end_date='', count=10, frequency='1d', adjust='qfq'):
    """港股日线（腾讯 hkfqkline 通道）：``hk00700`` 形式；成交额为万元，需 ×1e4。"""
    return _fetch_tx_market_daily(
        _normalize_hk_code_tx(code), 'hkfqkline',
        end_date=end_date, unit=_tx_kline_unit(frequency), count=count,
        fq=adjust if adjust in ('qfq', 'hfq') else 'qfq', amount_unit=10000.0,
    )


def get_price_day_tx_us(code, end_date='', count=10, frequency='1d', adjust='qfq'):
    """美股日线（腾讯 usfqkline 通道）：``usAAPL.OQ`` 形式，按交易所后缀回退。

    **回退判据是「覆盖度」而非「非空」**：某些标的（如 ETF 类的 AGQ / SCO）
    在首个候选后缀会返回 **200 + 非空数组，但只有当天 1 根**；若以「非空即返回」
    判定，会在首个候选就命中并返回，**永远不会尝试其余交易所后缀**。
    常规股（NTSK / GOOG）首个候选即可覆盖窗口，故不受影响。

    因此这里要求候选至少返回 ``_US_MIN_COVERAGE_RATIO`` 比例的请求根数才算命中，
    并在所有候选中取**行数最多**的结果；仅当全部候选覆盖度都达标才提前返回。
    覆盖度阈值取 0.5：容忍节假日/停牌造成的少量缺口，又能排除「只有 1 根」的伪命中。

    **注意（实测结论）**：对 ``AGQ``/``SCO`` 这类标的，腾讯接口 **``.OQ`` 与 ``.N``
    均只返回当天 1 根、``.A`` 返回 0 根**，且 ``qfqday``/``hfqday`` 恒为空（仅有不复权
    ``day``）——即腾讯侧**没有该标的的历史数据**，属数据源能力缺失而非本函数缺陷。
    此时本函数返回当天这 1 根（保证下游仍有当日数据可用），覆盖度不足由调用方
    ``_is_suspect_thin_kline`` 判定并回退东财；东财亦不可用时会在同步日志中如实告警。
    """
    unit = _tx_kline_unit(frequency)
    fq = adjust if adjust in ('qfq', 'hfq') else 'qfq'
    expected = max(1, min(int(count), _TX_MAX_COUNT))
    required = max(1, int(expected * _US_MIN_COVERAGE_RATIO))
    best_df = None
    candidates = _us_exchange_candidates(code)
    for candidate in candidates:
        df = _fetch_tx_market_daily(
            f'{_US_TX_PREFIX}{candidate}', 'usfqkline',
            end_date=end_date, unit=unit, count=count, fq=fq, amount_unit=1.0,
        )
        if df is None or df.empty:
            continue
        if best_df is None or len(df) > len(best_df):
            best_df = df
        if len(best_df) >= required:
            # 覆盖度已达标，无需再试其余后缀（省一次远端请求）
            return best_df
    if best_df is not None:
        return best_df
    raise ValueError(f'美股 {code} 在腾讯通道未取到数据（已尝试交易所后缀 {"/".join(candidates)}）')


def get_price(code, end_date='',count=10, frequency='1d', fields=[]):        #对外暴露只有唯一函数，这样对用户才是最友好的
    xcode = _normalize_ashare_code(code)

    if  frequency in ['1d','1w','1M']:   #1d日线  1w周线  1M月线
         try:    return get_price_sina( xcode, end_date=end_date,count=count,frequency=frequency)   #主力
         except Exception: return get_price_day_tx(xcode,end_date=end_date,count=count,frequency=frequency)   #备用

    if  frequency in ['1m','5m','15m','30m','60m']:  #分钟线 ,1m只有腾讯接口  5分钟5m   60分钟60m
         if frequency in '1m': return get_price_min_tx(xcode,end_date=end_date,count=count,frequency=frequency)
         try:    return get_price_sina(  xcode,end_date=end_date,count=count,frequency=frequency)   #主力
         except Exception: return get_price_min_tx(xcode,end_date=end_date,count=count,frequency=frequency)   #备用

if __name__ == '__main__':
    df=get_price('sh000001',frequency='1d',count=10)      #支持'1d'日, '1w'周, '1M'月
    print('上证指数日线行情\n',df)

    df=get_price('000001.XSHG',frequency='15m',count=10)  #支持'1m','5m','15m','30m','60m'
    print('上证指数分钟线\n',df)

# Ashare 股票行情数据( https://github.com/mpquant/Ashare )
