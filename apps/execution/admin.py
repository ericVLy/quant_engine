from django.contrib import admin
from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order


@admin.register(EventTypeRegistry)
class EventTypeRegistryAdmin(admin.ModelAdmin):
    list_display = ('name', 'scope', 'plugin_id', 'is_active', 'created_at')
    search_fields = ('name', 'description')
    list_filter = ('scope', 'is_active')


@admin.register(SuiteRun)
class SuiteRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'plan', 'suite', 'symbol', 'status', 'started_at', 'ended_at')
    list_filter = ('status',)
    search_fields = ('symbol',)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('id', 'run', 'event_type', 'source', 'status', 'created_at')
    list_filter = ('event_type', 'status')
    search_fields = ('source',)


@admin.register(ExecutionLog)
class ExecutionLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'symbol', 'plan', 'final_direction', 'status', 'trigger_time')
    list_filter = ('status', 'final_direction')
    search_fields = ('symbol',)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'symbol', 'direction', 'price', 'volume', 'status', 'created_at')
    list_filter = ('status', 'direction')
    search_fields = ('symbol',)