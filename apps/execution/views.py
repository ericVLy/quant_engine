from django.http import JsonResponse
import json
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order
from .serializers import (
    EventTypeRegistrySerializer, EventSerializer,
    SuiteRunSerializer, ExecutionLogSerializer, OrderSerializer
)
from .registry import EventRegistry
from .events import EventType
from .services import ExecutionError, process_next_event, start_suite_run, trigger_plan as create_plan_runs


# ============ 占位视图（临时） ============
def placeholder(request, message="占位接口"):
    return JsonResponse({'status': 'ok', 'message': message, 'app': 'execution'})


def trigger_plan(request):
    if request.method != 'POST':
        return JsonResponse({'detail': '只支持 POST 请求'}, status=405)

    try:
        data = json.loads(request.body or '{}')
        plan_id = data['plan_id']
        symbols = data.get('symbols') or [data.get('symbol')]
        symbols = [symbol for symbol in symbols if symbol]
        if not symbols:
            return JsonResponse({'detail': 'symbol 或 symbols 必填'}, status=400)
        runs = create_plan_runs(plan_id, symbols, data.get('payload'))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    except ExecutionError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)

    return JsonResponse({
        'status': 'ok',
        'run_ids': [run.id for run in runs],
    }, status=201)


def suite_run_status(request, run_id):
    run = get_object_or_404(SuiteRun, id=run_id)
    return JsonResponse({
        'status': 'ok',
        'run_id': run.id,
        'state': run.status,
        'symbol': run.symbol,
        'started_at': run.started_at,
        'ended_at': run.ended_at,
    })


def start_run(request, run_id):
    if request.method != 'POST':
        return JsonResponse({'detail': '只支持 POST 请求'}, status=405)
    run = get_object_or_404(SuiteRun, id=run_id)
    try:
        start_suite_run(run)
    except ExecutionError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    return JsonResponse({'status': 'ok', 'state': run.status})


def process_run_event(request, run_id):
    if request.method != 'POST':
        return JsonResponse({'detail': '只支持 POST 请求'}, status=405)
    run = get_object_or_404(SuiteRun, id=run_id)
    try:
        event = process_next_event(run)
    except ExecutionError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    if event is None:
        return JsonResponse({'status': 'ok', 'event': None})
    return JsonResponse({
        'status': 'ok',
        'event': {'id': event.id, 'type': event.event_type, 'state': event.status},
    })


# ============ API ViewSets ============

class EventTypeRegistryViewSet(viewsets.ModelViewSet):
    """事件类型注册表管理（管理员功能）"""
    queryset = EventTypeRegistry.objects.all()
    serializer_class = EventTypeRegistrySerializer
    permission_classes = [permissions.IsAdminUser]

    def perform_create(self, serializer):
        super().perform_create(serializer)
        EventRegistry.clear_cache()

    def perform_update(self, serializer):
        super().perform_update(serializer)
        EventRegistry.clear_cache()

    def perform_destroy(self, instance):
        instance.delete()
        EventRegistry.clear_cache()

    @action(detail=False, methods=['get'], url_path='list-all')
    def list_all(self, request):
        """列出所有事件类型（含系统内置 + 自定义）"""
        include_system = request.query_params.get('include_system', 'true') == 'true'
        data = EventRegistry.list_all(include_system=include_system)
        return Response(data)


class SuiteRunViewSet(viewsets.ReadOnlyModelViewSet):
    """SuiteRun 只读视图"""
    queryset = SuiteRun.objects.select_related('plan', 'suite').all().order_by('-created_at')
    serializer_class = SuiteRunSerializer
    filterset_fields = ['status', 'symbol', 'plan']


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    """Event 只读视图"""
    queryset = Event.objects.select_related('run').all().order_by('-created_at')
    serializer_class = EventSerializer
    filterset_fields = ['run', 'status', 'event_type']


class ExecutionLogViewSet(viewsets.ReadOnlyModelViewSet):
    """执行日志只读视图"""
    queryset = ExecutionLog.objects.select_related('plan').all().order_by('-trigger_time')
    serializer_class = ExecutionLogSerializer
    filterset_fields = ['symbol', 'plan', 'status']


class OrderViewSet(viewsets.ModelViewSet):
    """委托单 CRUD"""
    queryset = Order.objects.select_related('log').all().order_by('-created_at')
    serializer_class = OrderSerializer
    filterset_fields = ['symbol', 'status']