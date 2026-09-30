"""总览响应的 JSON 归一（类型安全兜底）。

响应契约由 :mod:`apps.dashboard.services` 显式构造的字典决定——**只有显式列出的
字段才会出现**（这是「字段白名单」的构造式实现），因此不存在"忘了剔除某个敏感
字段"的风险（资金块刻意不含 ``account_id``，见 N-05 / 规则 §12.8）。

本模块负责把 ``Decimal`` / ``datetime`` / ``date`` / ``tuple`` / ``set`` 等
非 JSON 原生类型统一转成前端可安全消费的形式：

- ``Decimal`` → 字符串（金额不做浮点损失）；
- ``datetime`` / ``date`` → ISO-8601；
- ``dict`` / 列表 → 递归归一，键统一为字符串。
"""
from datetime import date, datetime
from decimal import Decimal


def normalize(value):
    """递归归一为 JSON 可序列化结构（``Decimal``→str、日期→ISO-8601）。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, set):
        return sorted(normalize(item) for item in value)
    return value
