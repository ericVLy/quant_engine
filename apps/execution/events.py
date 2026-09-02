"""
事件类型定义与事件对象模型。

设计原则：
- `EventType` 负责维护系统内置事件类型常量。
- `BaseEvent` 负责定义事件对象的公共结构。
- 具体事件类（如 `SuiteInitEvent`）表示某一类事件；实例对象表示携带 payload 的具体事件。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


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


class BaseEvent:
    """事件对象基类，表示某个具体事件实例。"""

    event_type: str = ""

    def __init__(
        self,
        *,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        self.source = source
        self.payload = dict(payload or {})
        self.metadata = dict(metadata or {})
        self.extra = dict(kwargs)

        for key, value in self.extra.items():
            setattr(self, key, value)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "event_type": self.event_type,
            "source": self.source,
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
        }
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    def __str__(self) -> str:
        return f"{self.event_type}({self.source})"

    def summary(self) -> str:
        """默认摘要，子类可重写。"""
        return f"{self.event_type}: {self.source}"


class SystemStartEvent(BaseEvent):
    event_type = EventType.SYSTEM_START


class SystemStopEvent(BaseEvent):
    event_type = EventType.SYSTEM_STOP


class SuiteInitEvent(BaseEvent):
    event_type = EventType.SUITE_INIT

    def __init__(
        self,
        *,
        plan_id: Optional[int] = None,
        suite_id: Optional[int] = None,
        symbol: Optional[str] = None,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {'plan_id': plan_id, 'suite_id': suite_id, 'symbol': symbol}
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.plan_id = plan_id
        self.suite_id = suite_id
        self.symbol = symbol

    def summary(self) -> str:
        return f"SuiteInitEvent(symbol={self.symbol}, suite_id={self.suite_id})"


class SuiteStartEvent(BaseEvent):
    event_type = EventType.SUITE_START

    def __init__(
        self,
        *,
        run_id: Optional[int] = None,
        started_at: Optional[str] = None,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {'run_id': run_id, 'started_at': started_at}
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.run_id = run_id
        self.started_at = started_at


class SuiteCompletedEvent(BaseEvent):
    event_type = EventType.SUITE_COMPLETED


class SuiteFailedEvent(BaseEvent):
    event_type = EventType.SUITE_FAILED


class CaseStartEvent(BaseEvent):
    event_type = EventType.CASE_START

    def __init__(
        self,
        *,
        case_id: Optional[int] = None,
        case_name: str = "",
        trigger_event: str = "",
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {'case_id': case_id, 'case_name': case_name, 'trigger_event': trigger_event}
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.case_id = case_id
        self.case_name = case_name
        self.trigger_event = trigger_event


class CaseCompletedEvent(BaseEvent):
    event_type = EventType.CASE_COMPLETED

    def __init__(
        self,
        *,
        case_id: Optional[int] = None,
        result: Any = None,
        execution_time_ms: Optional[int] = None,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {'case_id': case_id, 'result': result, 'execution_time_ms': execution_time_ms}
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.case_id = case_id
        self.result = result
        self.execution_time_ms = execution_time_ms


class CaseFailedEvent(BaseEvent):
    event_type = EventType.CASE_FAILED


class CaseSkippedEvent(BaseEvent):
    event_type = EventType.CASE_SKIPPED


class TimerEvent(BaseEvent):
    event_type = EventType.TIMER

    def __init__(
        self,
        *,
        trigger_time: Optional[str] = None,
        interval_seconds: int = 0,
        cron: str = "",
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {'trigger_time': trigger_time, 'interval_seconds': interval_seconds, 'cron': cron}
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.trigger_time = trigger_time
        self.interval_seconds = interval_seconds
        self.cron = cron

    def is_due(self, current_time: str) -> bool:
        return self.trigger_time == current_time

    def summary(self) -> str:
        return f"TimerEvent(trigger_time={self.trigger_time}, interval={self.interval_seconds})"


class PriceSurgeEvent(BaseEvent):
    event_type = EventType.PRICE_SURGE

    def __init__(
        self,
        *,
        symbol: str = "",
        market: str = "A",
        price: Optional[float] = None,
        change_pct: float = 0.0,
        volume: int = 0,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {
            'symbol': symbol,
            'market': market,
            'price': price,
            'change_pct': change_pct,
            'volume': volume,
        }
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.symbol = symbol
        self.market = market
        self.price = price
        self.change_pct = change_pct
        self.volume = volume

    def is_upward(self) -> bool:
        return float(self.change_pct or 0) >= 0

    def summary(self) -> str:
        return f"PriceSurgeEvent({self.symbol}, price={self.price}, change_pct={self.change_pct})"


class PriceDropEvent(BaseEvent):
    event_type = EventType.PRICE_DROP

    def __init__(
        self,
        *,
        symbol: str = "",
        market: str = "A",
        price: Optional[float] = None,
        change_pct: float = 0.0,
        volume: int = 0,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {
            'symbol': symbol,
            'market': market,
            'price': price,
            'change_pct': change_pct,
            'volume': volume,
        }
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.symbol = symbol
        self.market = market
        self.price = price
        self.change_pct = change_pct
        self.volume = volume

    def is_downward(self) -> bool:
        return float(self.change_pct or 0) <= 0

    def summary(self) -> str:
        return f"PriceDropEvent({self.symbol}, price={self.price}, change_pct={self.change_pct})"


class VolumeSpikeEvent(BaseEvent):
    event_type = EventType.VOLUME_SPIKE

    def __init__(
        self,
        *,
        symbol: str = "",
        current_volume: int = 0,
        avg_volume: int = 0,
        spike_ratio: float = 0.0,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {
            'symbol': symbol,
            'current_volume': current_volume,
            'avg_volume': avg_volume,
            'spike_ratio': spike_ratio,
        }
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.symbol = symbol
        self.current_volume = current_volume
        self.avg_volume = avg_volume
        self.spike_ratio = spike_ratio

    def is_spike(self) -> bool:
        return float(self.spike_ratio or 0) > 1.0

    def summary(self) -> str:
        return f"VolumeSpikeEvent({self.symbol}, current_volume={self.current_volume}, avg_volume={self.avg_volume})"


class MacroCpiEvent(BaseEvent):
    event_type = EventType.MACRO_CPI

    def __init__(
        self,
        *,
        country: str = "",
        value: Optional[float] = None,
        released_at: Optional[str] = None,
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {'country': country, 'value': value, 'released_at': released_at}
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.country = country
        self.value = value
        self.released_at = released_at

    def summary(self) -> str:
        return f"MacroCpiEvent(country={self.country}, value={self.value})"


class MacroInterestEvent(BaseEvent):
    event_type = EventType.MACRO_INTEREST

    def __init__(
        self,
        *,
        country: str = "",
        rate: Optional[float] = None,
        policy: str = "",
        source: str = "",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        merged_payload = {'country': country, 'rate': rate, 'policy': policy}
        if payload:
            merged_payload.update(payload)
        super().__init__(source=source, payload=merged_payload, metadata=metadata, **kwargs)
        self.country = country
        self.rate = rate
        self.policy = policy

    def summary(self) -> str:
        return f"MacroInterestEvent(country={self.country}, policy={self.policy}, rate={self.rate})"


def get_event_class(event_type: str):
    """根据事件类型字符串返回对应的事件类。"""
    for cls in BaseEvent.__subclasses__():
        if cls.event_type == event_type:
            return cls
    return None


def build_event(event_type: str, **kwargs) -> BaseEvent:
    """根据事件类型快速构造具体事件对象。"""
    event_cls = get_event_class(event_type)
    if event_cls is None:
        raise ValueError(f"Unsupported event type: {event_type}")
    return event_cls(**kwargs)
