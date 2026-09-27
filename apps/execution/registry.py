from typing import Optional, Dict
from django.core.cache import cache
from .models import EventTypeRegistry


class EventRegistry:
    CACHE_KEY = "event_registry_cache"

    @classmethod
    def _registry_entry(cls, obj: EventTypeRegistry) -> dict:
        return {
            'scope': obj.scope,
            'plugin_id': obj.plugin_id,
            'description': obj.description,
            'payload_schema': obj.payload_schema or {},
            'base_event_type': obj.base_event_type or None,
        }

    @classmethod
    def _get_cache(cls) -> Dict[str, dict]:
        cached = cache.get(cls.CACHE_KEY)
        if cached is not None:
            return cached

        from .events import EventType
        registry = {}
        for et in EventType.all():
            registry[et] = {
                'scope': 'system',
                'plugin_id': None,
                'description': '系统内置事件',
                'payload_schema': {},
                'base_event_type': None,
            }
        for obj in EventTypeRegistry.objects.filter(is_active=True):
            registry[obj.name] = cls._registry_entry(obj)
        cache.set(cls.CACHE_KEY, registry, timeout=3600)
        return registry

    @classmethod
    def clear_cache(cls):
        cache.delete(cls.CACHE_KEY)

    @classmethod
    def validate(cls, event_type: str) -> bool:
        from .events import EventType

        registry = cls._get_cache()
        if EventType.is_valid(event_type):
            return True

        obj = EventTypeRegistry.objects.filter(
            name=event_type,
            is_active=True,
        ).first()
        if obj is None:
            return False

        registry[event_type] = cls._registry_entry(obj)
        cache.set(cls.CACHE_KEY, registry, timeout=3600)
        return True

    @classmethod
    def get(cls, event_type: str) -> Optional[dict]:
        registry = cls._get_cache()
        if event_type in registry:
            return registry[event_type]
        try:
            obj = EventTypeRegistry.objects.get(name=event_type, is_active=True)
        except EventTypeRegistry.DoesNotExist:
            return None
        info = cls._registry_entry(obj)
        registry[event_type] = info
        cache.set(cls.CACHE_KEY, registry, timeout=3600)
        return info

    @classmethod
    def get_base_event_type(cls, event_type: str) -> Optional[str]:
        """返回叠加事件的基事件类型；系统内置事件自身即基事件（返回 None）。"""
        info = cls.get(event_type)
        if info is None:
            return None
        return info.get('base_event_type') or None

    @classmethod
    def list_all(cls, include_system: bool = True) -> list:
        registry = cls._get_cache()
        result = []
        for event_type, info in registry.items():
            if include_system or info['scope'] != 'system':
                result.append({
                    'name': event_type,
                    'scope': info['scope'],
                    'description': info.get('description', ''),
                    'base_event_type': info.get('base_event_type'),
                })
        return sorted(result, key=lambda x: x['name'])

    @classmethod
    def _validate_registration(cls, scope: str, base_event_type: Optional[str]):
        """叠加约束：用户/插件事件只能叠加在系统自带事件之上（不允许套娃自定义事件）。"""
        from .events import EventType

        if scope == 'system':
            raise ValueError(
                "系统内置事件由代码定义（events.EventType），不允许注册 scope='system' 的事件类型"
            )
        if scope == 'user' and not base_event_type:
            raise ValueError("用户自定义事件必须叠加在系统自带事件之上：base_event_type 必填")
        if base_event_type and not EventType.is_valid(base_event_type):
            raise ValueError(
                f"叠加基事件 '{base_event_type}' 不是系统自带事件；"
                "仅支持叠加系统自带事件（不允许叠加其他自定义事件）"
            )

    @classmethod
    def register(cls, event_type: str, scope: str = 'user',
                 plugin_id: str = None, description: str = '',
                 payload_schema: dict = None, base_event_type: str = None):
        cls._validate_registration(scope, base_event_type)
        obj, created = EventTypeRegistry.objects.update_or_create(
            name=event_type,
            defaults={
                'scope': scope,
                'plugin_id': plugin_id,
                'description': description,
                'payload_schema': payload_schema or {},
                'base_event_type': base_event_type,
                'is_active': True,
            }
        )
        cls.clear_cache()
        return obj