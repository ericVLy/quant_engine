"""真实因子计算所依赖的常见技术指标（纯函数、无第三方依赖）。

所有函数接收数值序列并返回一个新序列或标量，保证对 ``dict`` 记录、
``pandas.Series`` 等可迭代输入均可用。空/过短序列会安全返回 ``None`` 或
给出明确结果，供上层 ``CaseExecutor`` 在数据不足时落入观望方向。
"""

from math import sqrt
from statistics import mean


def _clean(values):
    """将输入统一为数值列表，非有限数值（None/NaN/非数值）直接过滤。"""
    result = []
    for item in values or []:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float('inf'), float('-inf')):  # NaN/inf 排除
            continue
        result.append(number)
    return result


def moving_average(values, period):
    """简单移动平均，返回与输入等长的序列，前 ``period-1`` 个值为 None。"""
    values = _clean(values)
    if period < 1:
        raise ValueError('period 必须大于等于 1')
    out = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            out[index] = running / period
    return out


def exponential_moving_average(values, period):
    """指数移动平均，返回与输入等长的序列（None 位置保留对齐）。"""
    if period < 1:
        raise ValueError('period 必须大于等于 1')
    values = list(values or [])
    out = [None] * len(values)
    if not values:
        return out
    k = 2.0 / (period + 1)
    ema = None
    for index, value in enumerate(values):
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number:  # NaN
            continue
        if ema is None:
            ema = number
        else:
            ema = ema + k * (number - ema)
        out[index] = ema
    return out


def macd(values, fast=12, slow=26, signal=9):
    """MACD（DIF/DEA/柱），返回 ``(dif, dea, hist)`` 三条等长序列。"""
    fast_ema = exponential_moving_average(values, fast)
    slow_ema = exponential_moving_average(values, slow)
    dif = []
    for f, s in zip(fast_ema, slow_ema):
        if f is None or s is None:
            dif.append(None)
        else:
            dif.append(f - s)
    dea = exponential_moving_average(dif, signal)
    hist = []
    for d, e in zip(dif, dea):
        hist.append((d - e) * 2 if d is not None and e is not None else None)
    return dif, dea, hist


def rsi(values, period=14):
    """相对强弱指标，最后一位为最新 RSI（0~100），不足时返回 []。"""
    values = _clean(values)
    if len(values) < period + 1:
        return []
    gains = []
    losses = []
    for index in range(1, len(values)):
        diff = values[index] - values[index - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = mean(gains[-period:])
    avg_loss = mean(losses[-period:])
    return [100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)]


def kdj(highs, lows, closes, period=9):
    """KDJ，返回 ``(k, d, j)`` 最新三位标量。使用 3 日平滑。"""
    highs = _clean(highs)
    lows = _clean(lows)
    closes = _clean(closes)
    length = min(len(highs), len(lows), len(closes))
    if length < period:
        return None, None, None
    k_values = []
    d_value = 50.0
    for index in range(period - 1, length):
        window_high = max(highs[index - period + 1:index + 1])
        window_low = min(lows[index - period + 1:index + 1])
        if window_high == window_low:
            rsv = 50.0
        else:
            rsv = (closes[index] - window_low) / (window_high - window_low) * 100.0
        k = (2.0 / 3.0) * (k_values[-1] if k_values else 50.0) + (1.0 / 3.0) * rsv
        d_value = (2.0 / 3.0) * d_value + (1.0 / 3.0) * k
        k_values.append(k)
    k = k_values[-1]
    j = 3.0 * k - 2.0 * d_value
    return k, d_value, j


def bollinger(closes, period=20, num_std=2):
    """布林带，返回上/中/下轨最后三位标量。"""
    closes = _clean(closes)
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = mean(window)
    variance = sum((value - mid) ** 2 for value in window) / period
    std = sqrt(variance)
    return mid + num_std * std, mid, mid - num_std * std


def rate_of_change(values, period):
    """变动率（ROC），返回最新值，不足时返回 None。"""
    values = _clean(values)
    if len(values) <= period:
        return None
    if values[-period - 1] == 0:
        return 0.0
    return (values[-1] - values[-period - 1]) / values[-period - 1]


def pct_change(values):
    """相邻百分比变化，返回序列，首值为 None。"""
    values = _clean(values)
    out = [None] * len(values)
    for index in range(1, len(values)):
        if values[index - 1] == 0:
            out[index] = 0.0
        else:
            out[index] = (values[index] - values[index - 1]) / values[index - 1]
    return out


def volatility(values, period=20):
    """年化波动率（按日 252 交易日近似），返回最新值。"""
    changes = [c for c in pct_change(values) if c is not None]
    window = changes[-period:]
    if not window:
        return None
    center = mean(window)
    variance = sum((value - center) ** 2 for value in window) / len(window)
    return sqrt(variance) * sqrt(252)


def cross_above(fast, slow):
    """返回最新一位是否发生上穿（金叉）：前一位 fast<=slow 且最新 fast>slow。"""
    if len(fast) < 2 or len(slow) < 2:
        return False
    if None in (fast[-1], fast[-2], slow[-1], slow[-2]):
        return False
    return fast[-2] <= slow[-2] and fast[-1] > slow[-1]


def cross_below(fast, slow):
    """返回最新一位是否发生下穿（死叉）：前一位 fast>=slow 且最新 fast<slow。"""
    if len(fast) < 2 or len(slow) < 2:
        return False
    if None in (fast[-1], fast[-2], slow[-1], slow[-2]):
        return False
    return fast[-2] >= slow[-2] and fast[-1] < slow[-1]