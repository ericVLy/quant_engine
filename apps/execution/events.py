"""
事件类型常量定义
所有系统内置事件类型集中管理，避免硬编码字符串
"""


class EventType:
    """系统内置事件类型常量"""
    # 系统级
    SYSTEM_START = "SYSTEM_START"
    SYSTEM_STOP = "SYSTEM_STOP"

    # Suite 生命周期
    SUITE_INIT = "SUITE_INIT"
    SUITE_START = "SUITE_START"
    SUITE_COMPLETED = "SUITE_COMPLETED"
    SUITE_FAILED = "SUITE_FAILED"

    # Case 生命周期
    CASE_START = "CASE_START"
    CASE_COMPLETED = "CASE_COMPLETED"
    CASE_FAILED = "CASE_FAILED"
    CASE_SKIPPED = "CASE_SKIPPED"

    # 时间
    TIMER = "TIMER"

    # 外部事件（插件触发）
    PRICE_SURGE = "PRICE_SURGE"
    PRICE_DROP = "PRICE_DROP"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    MACRO_CPI = "MACRO_CPI"
    MACRO_INTEREST = "MACRO_INTEREST"

    @classmethod
    def all(cls) -> list:
        """获取所有预定义事件类型名称列表"""
        return [
            getattr(cls, name)
            for name in dir(cls)
            if not name.startswith("_") and isinstance(getattr(cls, name), str)
        ]

    @classmethod
    def is_valid(cls, event_type: str) -> bool:
        """检查是否为内置事件类型"""
        return event_type in cls.all()