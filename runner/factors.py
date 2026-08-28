from statistics import mean


def values_from(data, field):
    if hasattr(data, 'to_dict'):
        data = data.to_dict('records')
    return [row[field] for row in data if isinstance(row, dict) and field in row]


def calculate(params, context):
    """Calculate small, deterministic factors usable by the default CaseExecutor."""
    operation = params.get('calculation')
    if not operation:
        return None
    values = values_from(context.get('market_data', []), params.get('field', 'close'))
    if not values:
        return {'direction': 0, 'payload': {'data_empty': True}}
    if operation == 'last':
        value = values[-1]
    elif operation == 'mean':
        value = mean(values[-params.get('period', len(values)):])
    elif operation == 'compare':
        value = values[-1]
        direction = 1 if value > params.get('threshold', 0) else -1 if value < params.get('threshold', 0) else 0
        return {'direction': direction, 'payload': {'value': value}}
    else:
        raise ValueError(f'不支持的 calculation: {operation}')
    return {'direction': 0, 'payload': {'value': value}}