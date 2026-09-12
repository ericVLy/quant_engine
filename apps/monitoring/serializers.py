from rest_framework import serializers

from zoneinfo import ZoneInfo

from .market_calendar import market_timezone
from .models import IntradayPoint


class IntradayPointSerializer(serializers.ModelSerializer):
    """分时监控点序列化器。

    - ``ts`` 输出 UTC ISO-8601（如 ``2026-09-10T01:31:00Z``），前端无需二次换算；
    - ``local_time`` 为市场本地时间（HH:MM），X 轴渲染直接使用。
    """

    ts = serializers.SerializerMethodField()
    local_time = serializers.SerializerMethodField()

    class Meta:
        model = IntradayPoint
        fields = (
            'ts', 'local_time', 'price', 'change', 'volume', 'amount',
            'avg_price', 'high', 'low', 'open_price', 'pre_close',
        )

    def get_ts(self, obj):
        return obj.ts.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ')

    def get_local_time(self, obj):
        return obj.ts.astimezone(market_timezone(obj.symbol.market)).strftime('%H:%M')