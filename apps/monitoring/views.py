import json
import time

from rest_framework import serializers as drf_serializers
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django.http import JsonResponse, StreamingHttpResponse
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

    def _collect_payload(self, symbol, with_realtime=True):
        points = list(
            IntradayPoint.objects.select_related('symbol')
            .filter(symbol=symbol).order_by('ts')
        )
        realtime = None
        if with_realtime:
            realtime = self._snapshot_payload(getattr(symbol, 'snapshot', None))
        return self._base(symbol, points, realtime=realtime)
# ---------------------------------------------------------------------- #
# SSE 持久化推送（前后端交互由轮询改为持久连接）
#
# 必须是普通 Django 视图而非 DRF action：EventSource 发送
# ``Accept: text/event-stream``，DRF 内容协商会拒绝并返回 406 Not Acceptable。
# 本视图不走 DRF，直接以 ``StreamingHttpResponse`` 输出 SSE。
# ---------------------------------------------------------------------- #

def intraday_stream(request):
    """``GET /api/monitoring/intraday/stream/?symbol=000001``（SSE）。

    - 选择 SSE 而非 WebSocket：本服务为纯 WSGI Django（无 channels/daphne），
      分时是服务端单向持续推送场景，``StreamingHttpResponse`` 零新依赖即可满足；
    - 事件：``snapshot``（连接建立后的当日全量）→ 周期 ``tick``（全量快照，
      前端按 ts 增量合并）→ ``session_status`` 变化时 ``session`` 事件；
    - ``interval`` 推送间隔（秒），范围 5~60，缺省 15；
    - 客户端断开（GeneratorExit）即结束，不残留线程。
    """
    code = request.GET.get('symbol')
    if not code:
        return JsonResponse({'detail': 'symbol 参数必填'}, status=400)
    symbol = get_object_or_404(Symbol, code=code)
    try:
        interval = int(request.GET.get('interval', 15))
    except (TypeError, ValueError):
        interval = 15
    interval = min(max(interval, 5), 60)

    response = StreamingHttpResponse(
        _event_stream(symbol, interval),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def _collect_payload_for(symbol, with_realtime=True):
    points = list(
        IntradayPoint.objects.select_related('symbol')
        .filter(symbol=symbol).order_by('ts')
    )
    realtime = _snapshot_payload_for(getattr(symbol, 'snapshot', None)) if with_realtime else None
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
        'points': IntradayPointSerializer(points, many=True).data,
        'realtime': realtime,
    }


def _snapshot_payload_for(snapshot):
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


def _event_stream(symbol, interval):
    def format_event(event, data):
        return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

    try:
        # 连接建立即推送一次当日全量
        payload = _collect_payload_for(symbol)
        last_status = payload.get('session_status')
        yield format_event('snapshot', payload)

        while True:
            time.sleep(interval)
            payload = _collect_payload_for(symbol)
            status = payload.get('session_status')
            if status != last_status:
                last_status = status
                yield format_event('session', {
                    'symbol': symbol.code,
                    'session_status': status,
                })
            yield format_event('tick', payload)
    except GeneratorExit:
        # 客户端断开：正常结束流
        raise
    except Exception as exc:  # pylint: disable=broad-except
        yield format_event('error', {'detail': str(exc)})
