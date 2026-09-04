"""真实因子计算编排器。

提供三类能力，均以 ``calculate(params, context)`` 统一入口供
``CaseExecutor`` 调用：

- ``signal``：从行情数据计算单个技术指标并裁决买卖方向；
- ``filter``：对已计算的方向做阈值/条件过滤（命中则保持方向，否则观望）；
- ``verdict``：将多个子因子结果按权重/阈值综合裁决为最终方向。

保持对旧 ``calculation`` 接口（``last`` / ``mean`` / ``compare``）的向后兼容。
"""

from statistics import mean

from . import indicators as ind


def values_from(data, field):
    """从多种数据载体中安全提取 ``field`` 对应数值序列。

    支持 list[dict]、pandas DataFrame 及任何具备 ``to_dict`` 的对象。
    """
    if hasattr(data, 'to_dict'):
        data = data.to_dict('records')
    return [row[field] for row in data if isinstance(row, dict) and field in row]


def _series(context, field='close'):
    """从 Case 上下文提取数值序列，缺省字段时退化为最近价格。"""
    values = values_from(context.get('market_data', []), field)
    if not values:
        last = context.get('last_close') or context.get('price')
        if last is not None:
            return [float(last)]
    return values


def _period(params, default):
    value = params.get('period', default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _threshold(params, keys=('threshold',)):
    for key in keys:
        value = params.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _direction_value(value, up=1, down=-1):
    """将标量阈值比较映射为方向。"""
    if value > 0:
        return up
    if value < 0:
        return down
    return 0
def _signal(params, context):
    """依据 ``indicator`` 计算指标并裁决方向。"""
    indicator = params.get('indicator', params.get('calculation'))
    field = params.get('field', 'close')
    values = _series(context, field)
    payload = {'indicator': indicator, 'field': field, 'data_empty': not bool(values)}

    closes = values
    if indicator in ('ma', 'sma', 'mean'):
        ma = ind.moving_average(closes, _period(params, 20))
        if ma and ma[-1] is not None:
            value = closes[-1] - ma[-1]
            return {'direction': _direction_value(value), 'payload': {**payload, 'ma': ma[-1], 'value': closes[-1]}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator == 'ema':
        ema = ind.exponential_moving_average(closes, _period(params, 20))
        if ema and ema[-1] is not None:
            value = closes[-1] - ema[-1]
            return {'direction': _direction_value(value), 'payload': {**payload, 'ema': ema[-1], 'value': closes[-1]}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator == 'macd':
        fast = _period({'period': params.get('fast')}, 12)
        slow = _period({'period': params.get('slow')}, 26)
        signal_period = _period({'period': params.get('signal')}, 9)
        dif, dea, hist = ind.macd(closes, fast, slow, signal_period)
        if hist and hist[-1] is not None and dea and dea[-1] is not None:
            if ind.cross_above(dif, dea) and hist[-1] > 0:
                return {'direction': 1, 'payload': {**payload, 'dif': dif[-1], 'dea': dea[-1], 'hist': hist[-1], 'cross': 'golden'}}
            if ind.cross_below(dif, dea) and hist[-1] < 0:
                return {'direction': -1, 'payload': {**payload, 'dif': dif[-1], 'dea': dea[-1], 'hist': hist[-1], 'cross': 'dead'}}
            return {'direction': _direction_value(hist[-1]), 'payload': {**payload, 'dif': dif[-1], 'dea': dea[-1], 'hist': hist[-1], 'cross': 'none'}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator == 'rsi':
        period = _period(params, 14)
        rsi_values = ind.rsi(closes, period)
        if rsi_values:
            value = rsi_values[-1]
            overbought = _threshold(params, ('threshold_overbought', 'threshold'))
            oversold = _threshold(params, ('threshold_oversold',))
            if oversold is not None and value < oversold:
                direction = 1
            elif overbought is not None and value > overbought:
                direction = -1
            else:
                direction = 0
            return {'direction': direction, 'payload': {**payload, 'rsi': value, 'period': period}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator == 'kdj':
        high_field = params.get('high_field', 'high')
        low_field = params.get('low_field', 'low')
        k, d, j = ind.kdj(
            _series(context, high_field), _series(context, low_field), closes,
            _period(params, 9),
        )
        if k is not None:
            oversold = _threshold(params, ('threshold_oversold',))
            overbought = _threshold(params, ('threshold_overbought',))
            if (d is not None) and (k - d) > 0 and (oversold is None or k > oversold):
                direction = 1
            elif (d is not None) and (k - d) < 0 and (overbought is None or k < overbought):
                direction = -1
            else:
                direction = 0
            return {'direction': direction, 'payload': {**payload, 'k': k, 'd': d, 'j': j}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator == 'boll':
        upper, mid, lower = ind.bollinger(closes, _period(params, 20))
        if upper is not None:
            last = closes[-1]
            direction = 1 if last <= lower else -1 if last >= upper else 0
            return {'direction': direction, 'payload': {**payload, 'upper': upper, 'mid': mid, 'lower': lower}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator in ('roc', 'momentum'):
        value = ind.rate_of_change(closes, _period(params, 10))
        if value is not None:
            return {'direction': _direction_value(value), 'payload': {**payload, 'roc': value}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator == 'pct_change':
        changes = ind.pct_change(closes)
        if changes and changes[-1] is not None:
            threshold = _threshold(params, ('threshold',))
            if threshold is not None:
                return {'direction': _direction_value(changes[-1] - threshold), 'payload': {**payload, 'change_pct': changes[-1]}}
            return {'direction': _direction_value(changes[-1]), 'payload': {**payload, 'change_pct': changes[-1]}}
        return {'direction': 0, 'payload': {**payload, 'insufficient': True}}

    if indicator == 'volatility':
        value = ind.volatility(closes, _period(params, 20))
        return {'direction': 0, 'payload': {**payload, 'volatility': value}}

    # 旧接口支持
    return _legacy_calculation(params, context, payload)


def _legacy_calculation(params, context, payload):
    """保留对 historical ``last`` / ``mean`` / ``compare`` 的兼容。"""
    operation = params.get('calculation')
    values = values_from(context.get('market_data', []), params.get('field', 'close'))
    if not values:
        return {'direction': 0, 'payload': {**payload, 'data_empty': True}}
    if operation == 'last':
        value = values[-1]
    elif operation == 'mean':
        value = mean(values[-_period(params, len(values)):])
    elif operation == 'compare':
        value = values[-1]
        threshold = _threshold(params, ('threshold',))
        if threshold is None:
            threshold = 0
        direction = 1 if value > threshold else -1 if value < threshold else 0
        return {'direction': direction, 'payload': {**payload, 'value': value}}
    else:
        raise ValueError(f'不支持的 calculation: {operation}')
    return {'direction': 0, 'payload': {**payload, 'value': value}}


def _filter(params, context, signal_result):
    """对既定方向应用过滤条件，命中则保留方向，否则观望。"""
    filter_cfg = params.get('filter') if isinstance(params.get('filter'), dict) else {}
    payload = dict(signal_result.get('payload', {}) or {})
    direction = int(signal_result.get('direction', 0))

    if not filter_cfg:
        return signal_result

    op = filter_cfg.get('op', 'keep')
    threshold = _threshold(filter_cfg, ('threshold', 'value'))
    field = filter_cfg.get('field')
    if field and threshold is not None:
        metric = payload.get(field)
        if metric is None:
            series = _series(context, field)
            metric = series[-1] if series else None
        if metric is not None:
            try:
                metric = float(metric)
            except (TypeError, ValueError):
                metric = None
        passes = metric is not None and (
            (op == 'keep' and metric >= threshold) or
            (op == 'drop' and metric < threshold)
        )
    else:
        passes = direction != 0

    filtered = direction if (direction != 0 and passes) else 0
    payload['filtered'] = not (direction == filtered and direction != 0)
    return {'direction': filtered, 'payload': payload}


def _verdict(params, context):
    """将配置的子因子结果按权重/投票综合裁决，返回最终方向。"""
    verdict_cfg = params.get('verdict') if isinstance(params.get('verdict'), dict) else {}
    components = verdict_cfg.get('components', [])
    if not components:
        raise ValueError('verdict 必须提供 components 列表')

    directions = []
    for component in components:
        sub_params = dict(params)
        sub_params.pop('node_type', None)
        sub_params.pop('verdict', None)
        sub_params.update(component)
        result = _signal(sub_params, context)
        weight = float(component.get('weight', 1.0))
        directions.append((int(result.get('direction', 0)), weight))

    method = verdict_cfg.get('method', 'weighted_sum')
    if method == 'vote':
        tally = {d: sum(w for d0, w in directions if d0 == d) for d in (-1, 0, 1)}
        final_direction = max(tally, key=lambda d: (tally[d], -abs(d)))
    else:
        total = sum(d * w for d, w in directions)
        final_direction = 1 if total > 0 else -1 if total < 0 else 0

    return {
        'direction': final_direction,
        'payload': {
            'verdict': method,
            'components': [{'direction': d, 'weight': w} for d, w in directions],
            'total': sum(d * w for d, w in directions),
        },
    }


def calculate(params, context):
    """按节点类型执行信号计算、过滤和综合裁决。"""
    if params.get('verdict'):
        return _verdict(params, context)

    node_type = params.get('node_type')
    result = _signal(params, context)

    if result.get('payload', {}).get('data_empty') and node_type == 'filter':
        return {'direction': 0, 'payload': {**result.get('payload', {}), 'filtered': True}}

    if node_type == 'filter' or params.get('filter'):
        result = _filter(params, context, result)
    return result