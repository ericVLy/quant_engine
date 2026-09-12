from rest_framework import serializers as drf_serializers
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.watchlists.models import Symbol

from .market_calendar import MARKET_TIMEZONES, session_status, to_market_local
from .models import IntradayPoint
from .serializers import IntradayPointSerializer


class IntradayViewSet(viewsets.GenericViewSet):
    """分时监控数据 API。

    - ``GET /api/monitoring/intraday/?symbol=000001`` 当日分时序列（时间升序）；
    - ``GET /api/monitoring/intraday/realtime/?symbol=000001`` 最新一条 + RealtimeSnapshot 合并。

    单标的单日 ~240 个点，**不走分页**（对齐模块9 设计）。
    """

    serializer_class = IntradayPointSerializer

    def _symbol(self):
        code = self.request.query_params.get('symbol')
        if not code:
            raise drf_serializers.ValidationError({'detail': 'symbol 参数必填'})
        return get_object_or_404(Symbol, code=code)

    def _base(self, symbol, points, realtime=None):
        now_local = to_market_local(timezone.now(), symbol.market)
        latest = points[-1] if points else None
        pre_close = None
        if latest is not None and latest.pre_close is not None:
            pre_close = str(latest.pre_close)
        elif realtime and realtime.get('pre_close'):
            pre_close = realtime['pre_close']
        return {
            'symbol': symbol.code,
            'market': symbol.market,
            'timezone': MARKET_TIMEZONES[symbol.market],
            'session_status': session_status(symbol.market, now_local),
            'pre_close': pre_close,
            'points': IntradayPointSerializer(
                points, many=True, context={'request': self.request},
            ).data,
            'realtime': realtime,
        }

    @staticmethod
    def _snapshot_payload(snapshot):
        if snapshot is None:
            return None
        return {
            'price': str(snapshot.price),
            'change': str(snapshot.change),
            'volume': snapshot.volume,
            'turnover': str(snapshot.turnover),
            'high': str(snapshot.high) if snapshot.high is not None else None,
            'low': str(snapshot.low) if snapshot.low is not None else None,
            'open_price': str(snapshot.open_price) if snapshot.open_price is not None else None,
            'pre_close': str(snapshot.pre_close) if snapshot.pre_close is not None else None,
            'updated_at': snapshot.updated_at.isoformat() if snapshot.updated_at else None,
        }

    def list(self, request):
        symbol = self._symbol()
        points = list(
            IntradayPoint.objects.select_related('symbol')
            .filter(symbol=symbol).order_by('ts')
        )
        return Response(self._base(symbol, points))

    @action(detail=False, methods=['get'], url_path='realtime')
    def realtime(self, request):
        symbol = self._symbol()
        point = (
            IntradayPoint.objects.select_related('symbol')
            .filter(symbol=symbol).order_by('-ts').first()
        )
        snapshot = getattr(symbol, 'snapshot', None)
        data = self._base(symbol, [point] if point else [], realtime=self._snapshot_payload(snapshot))
        return Response(data)