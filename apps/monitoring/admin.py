from django.contrib import admin

from .models import IntradayPoint


@admin.register(IntradayPoint)
class IntradayPointAdmin(admin.ModelAdmin):
    list_display = ('id', 'symbol', 'ts', 'price', 'change', 'volume')
    list_filter = ('symbol__market', 'ts')
    search_fields = ('symbol__code',)
    ordering = ('-ts',)