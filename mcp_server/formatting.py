"""JSON-safe serialization for MCP tool outputs."""
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def to_jsonable(value: Any) -> Any:
    """把工具返回值归一为 JSON 安全结构（MCP 结构化输出不含自定义类型）。

    Args:
        value: 任意工具输出片段，可为模型实例、``Decimal``、日期或嵌套容器。

    Returns:
        Any: ``None`` / ``bool`` / ``int`` / ``float`` / ``str`` 原样返回；
        ``Decimal`` → 字符串（与项目 DecimalField 序列化一致）；``date`` / ``datetime``
        → ISO-8601 字符串；``dict`` 键转字符串并递归归一；``list`` / ``tuple`` →
        列表递归归一；模型实例 → 主键 ``pk``；其余对象 → ``str(value)``。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, 'pk'):
        return value.pk
    return str(value)
