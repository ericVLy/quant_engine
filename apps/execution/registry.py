from typing import Optional, Dict
from django.core.cache import cache
from .models import EventTypeRegistry


class EventRegistry:
    CACHE_KEY = "event_registry_cache"

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
                'payload_schema': {}
            }
        for obj in EventTypeRegistry.objects.filter(is_active=True):
            registry[obj.name] = {
                'scope': obj.scope,
                'plugin_id': obj.plugin_id,
                'description': obj.description,
                'payload_schema': obj.payload_schema or {}
            }
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

        registry[event_type] = {
            'scope': obj.scope,
            'plugin_id': obj.plugin_id,
            'description': obj.description,
            'payload_schema': obj.payload_schema or {},
        }
        cache.set(cls.CACHE_KEY, registry, timeout=3600)
        return True

    @classmethod
    def get(cls, event_type: str) -> Optional[dict]:
        registry = cls._get_cache()
        if event_type in registry:
            return registry[event_type]
        try:
            obj = EventTypeRegistry.objects.get(name=event_type, is_active=True)
            info = {
                'scope': obj.scope,
                'plugin_id': obj.plugin_id,
                'description': obj.description,
                'payload_schema': obj.payload_schema or {}
            }
            registry[event_type] = info
            cache.set(cls.CACHE_KEY, registry, timeout=3600)
            return info
        except EventTypeRegistry.DoesNotExist:
            return None

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
                })
        return sorted(result, key=lambda x: x['name'])

    @classmethod
    def register(cls, event_type: str, scope: str = 'user',
                 plugin_id: str = None, description: str = '',
                 payload_schema: dict = None):
        obj, created = EventTypeRegistry.objects.update_or_create(
            name=event_type,
            defaults={
                'scope': scope,
                'plugin_id': plugin_id,
                'description': description,
                'payload_schema': payload_schema or {},
                'is_active': True,
            }
        )
        cls.clear_cache()
        return obj