"""多市场时区与交易时段判定（模块9）。

- A 股   : Asia/Shanghai
- 港股   : Asia/Hong_Kong
- 美股   : America/New_York（zoneinfo 自动处理 EDT/EST 夏令时切换）

交易时段：
  A  [('09:30', '11:30'), ('13:00', '15:00')]
  HK [('09:30', '12:00'), ('13:00', '16:00')]
  US [('09:30', '16:00')]          # 连续时段，无午休

约定（不含节假日历，按工作日 + 时段简化）：
- trading     交易时段内
- lunch_break 交易日内两个交易时段之间（午休；仅双时段市场可达）
- pre_market  交易日内开盘前
- closed      收盘后 / 周末 / 开盘前非交易日
"""
from datetime import timedelta
from zoneinfo import ZoneInfo

MARKET_TIMEZONES = {
    'A': 'Asia/Shanghai',
    'HK': 'Asia/Hong_Kong',
    'US': 'America/New_York',
}

TRADING_SESSIONS = {
    'A': [('09:30', '11:30'), ('13:00', '15:00')],
    'HK': [('09:30', '12:00'), ('13:00', '16:00')],
    'US': [('09:30', '16:00')],
}

_SESSION_STATUS_CHOICES = ('trading', 'lunch_break', 'pre_market', 'closed')


def _to_minutes(hhmm):
    hour, minute = hhmm.split(':')
    return int(hour) * 60 + int(minute)


def _sessions(market):
    return [(_to_minutes(start), _to_minutes(end)) for start, end in TRADING_SESSIONS[market]]


def market_timezone(market):
    """返回市场本地时区（``zoneinfo.ZoneInfo``）。"""
    return ZoneInfo(MARKET_TIMEZONES[str(market).upper()])


def to_market_local(dt, market):
    """将任意 datetime 转为市场本地时间的 naive datetime。"""
    from django.utils import timezone

    if dt.tzinfo is None:
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(market_timezone(market)).replace(tzinfo=None)


def session_status(market, now_local):
    """返回市场本地时间 ``now_local`` 的会话状态。

    ``now_local`` 必须是市场本地时间（naive 或 aware 均可，内部取 hour/minute/weekday）。
    """
    market = str(market).upper()
    if market not in TRADING_SESSIONS:
        raise ValueError(f'不支持的市场: {market}')

    # 周末一律 closed（不发节假日历，避免无效空转）
    if now_local.weekday() >= 5:
        return 'closed'

    sessions = _sessions(market)
    minute = now_local.hour * 60 + now_local.minute
    first_open = sessions[0][0]
    last_close = sessions[-1][1]

    if minute < first_open:
        return 'pre_market'
    for start, end in sessions:
        if start <= minute < end:
            return 'trading'
    if minute < last_close:
        return 'lunch_break'
    return 'closed'


def in_trading_session(market, now_local):
    """``now_local`` 处于交易时段内返回 True（午休 / 开盘前 / 收盘 / 周末均 False）。"""
    return session_status(market, now_local) == 'trading'


def trading_minutes_local(market, now_local):
    """返回 ``now_local``（market 本地、naive）当日已开启的交易分钟（naive 本地分钟，升序）。

    - 周末返回空；开盘前返回空；
    - 每个交易时段取 ``[开盘, min(收盘, now_local)]`` 的分钟（含当前已开启的那一分钟）；
    - 午休间隙不输出，保证分时数据只在交易时段有值。
    """
    market = str(market).upper()
    now_local = now_local.replace(second=0, microsecond=0)
    if now_local.weekday() >= 5:
        return []
    now_minute = now_local.hour * 60 + now_local.minute
    first_open = _to_minutes(TRADING_SESSIONS[market][0][0])
    if now_minute < first_open:
        return []
    base = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for start, end in _sessions(market):
        if start >= now_minute:
            continue
        upto = min(end, now_minute + 1)
        for minute in range(start, upto):
            out.append(base + timedelta(minutes=minute))
    return out