# pylint: disable=import-outside-toplevel,too-many-ancestors  # 延迟导入以规避循环依赖/加载期副作用；DRF ModelViewSet 混入多个 mixin，继承链天然深
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings as django_settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order, FundAllocation,
                    Alert, AlertChannel, NodeRun, AccountFundConfig)
from .serializers import (
    EventTypeRegistrySerializer, EventSerializer,
    SuiteRunSerializer, ExecutionLogSerializer, OrderSerializer,
    FundAllocationSerializer, AlertSerializer, AlertChannelSerializer, AlertActionSerializer,
    NodeRunSerializer, AccountFundConfigSerializer,
)
from .fund_sync import mask_account
from .redaction import redact_text
from .registry import EventRegistry
from .services import ExecutionError, process_next_event, start_suite_run, trigger_plan as create_plan_runs

logger = logging.getLogger(__name__)


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
        page = self.paginate_queryset(data)
        if page is not None:
            return self.get_paginated_response(page)
        return Response(data)


class SuiteRunViewSet(viewsets.ReadOnlyModelViewSet):
    """SuiteRun 只读视图"""
    queryset = SuiteRun.objects.select_related('plan', 'suite').all().order_by('-created_at')
    serializer_class = SuiteRunSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['status', 'symbol', 'plan', 'suite']

    @action(detail=True, methods=['get'], url_path='node-runs')
    def node_runs(self, request, pk=None):
        """返回某次运行的节点执行轨迹（按执行顺序，供前端回放）。"""
        node_runs = (
            NodeRun.objects.filter(run_id=pk)
            .select_related('suite', 'case', 'parent')
            .order_by('id')
        )
        return Response(NodeRunSerializer(node_runs, many=True).data)


class NodeRunViewSet(viewsets.ReadOnlyModelViewSet):
    """NodeRun 只读视图（编排树节点运行实例 / 轨迹回放）"""
    queryset = NodeRun.objects.select_related('run', 'suite', 'case', 'parent').all().order_by('started_at', 'id')
    serializer_class = NodeRunSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['run', 'node_type', 'status', 'suite', 'case']


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    """Event 只读视图"""
    queryset = Event.objects.select_related('run').all().order_by('-created_at')
    serializer_class = EventSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['run', 'status', 'event_type']


class ExecutionLogViewSet(viewsets.ReadOnlyModelViewSet):
    """执行日志只读视图"""
    queryset = ExecutionLog.objects.select_related('plan').all().order_by('-trigger_time')
    serializer_class = ExecutionLogSerializer
    filterset_fields = ['symbol', 'plan', 'status']


def _sync_timeout_seconds():
    """同步子进程的超时（秒）。取 ``ACCOUNT_SYNC_TIMEOUT_SECONDS`` 并留出启动余量。"""
    base = getattr(django_settings, 'ACCOUNT_SYNC_TIMEOUT_SECONDS', None)
    if base is None:
        base = int(os.getenv('ACCOUNT_SYNC_TIMEOUT_SECONDS', '20'))
    # 子进程还要完成 Django 启动与数据库连接，多留 15s
    return int(base) + 15


def run_account_sync_in_subprocess(account_id, capital_basis='total'):
    """在**独立子进程**里执行 gm 同步，超时即杀掉。

    ⚠️ 为什么必须进程隔离（本项目踩过的最隐蔽的坑）：

    实测（真实 gm SDK + 本地黑洞端口）：终端关闭时 ``gm.api.get_cash()``
    会**持有 GIL 一直阻塞**——同进程内的心跳线程完全停摆，主线程连
    ``join(timeout)`` 的超时逻辑都执行不到。也就是说**任何"同进程加超时"的
    方案都是无效的**：整个 Web 进程被冻结，表现为「浏览器一直转圈、整站打不开」。

    子进程被卡住时只影响它自己；``subprocess.run(timeout=...)`` 会 ``kill()``
    掉它（SIGKILL 由操作系统处理，不依赖目标进程能响应），Web 进程毫发无损，
    连接也能正常释放。

    Args:
        account_id: gm user id。
        capital_basis: 额度口径。

    Returns:
        dict: ``{'ok': bool, 'detail': str}``；``ok=False`` 时 ``detail`` 可直接展示。
    """
    base_dir = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    # 子进程是非 Web 命令：显式关闭分时更新器，避免重复外部请求与写库
    env.setdefault('MONITORING_UPDATER_ENABLED', '0')
    env.setdefault('DJANGO_SETTINGS_MODULE', 'quant_engine.settings.dev')
    cmd = [
        sys.executable, str(base_dir / 'manage.py'), 'sync_account_funds',
        '--account-id', account_id, '--capital-basis', capital_basis,
    ]
    timeout = _sync_timeout_seconds()
    logger.info('账户 %s 同步：启动子进程（超时 %ss）', mask_account(account_id), timeout)
    started = time.monotonic()
    try:
        proc = subprocess.run(  # 固定参数列表，无 shell 注入面
            cmd, cwd=str(base_dir), env=env, timeout=timeout,
            capture_output=True, text=True, check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning('账户 %s 同步子进程超时（%ss）已终止，Web 进程不受影响',
                       mask_account(account_id), timeout)
        return {
            'ok': False,
            'detail': f'gm 终端无响应（{timeout}s 内未返回），已终止同步；'
                      '请确认掘金终端已启动并登录后再试',
        }
    except Exception as exc:  # pylint: disable=broad-except
        logger.error('账户 %s 同步子进程异常：%s', mask_account(account_id), exc)
        return {'ok': False, 'detail': f'同步进程异常：{redact_text(str(exc))}'}

    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-1:]
        logger.warning('账户 %s 同步失败（rc=%s，耗时 %.1fs）：%s',
                       mask_account(account_id), proc.returncode, elapsed,
                       tail[0] if tail else '')
        return {
            'ok': False,
            'detail': redact_text(tail[0]) if tail else f'同步失败（退出码 {proc.returncode}）',
        }
    logger.info('账户 %s 同步子进程完成，耗时 %.1fs', mask_account(account_id), elapsed)
    return {'ok': True, 'detail': ''}


class AccountFundConfigViewSet(viewsets.ModelViewSet):
    """gm 账户预配置（gm user id → 资金/持仓快照）。

    Plan 的资金占用按 ``account_id``（即 gm user id）匹配到本表的一行；
    资金与持仓字段只读，由 ``sync`` 动作从 gm 拉取后写入。

    账户配置含账户 ID（半敏感），按 N-05 收敛为**管理员可写、登录用户只读**。
    """

    queryset = AccountFundConfig.objects.all().order_by('account_id')
    serializer_class = AccountFundConfigSerializer
    filterset_fields = ['is_active', 'source', 'capital_basis']

    def get_permissions(self):
        """写操作（增删改、立即同步）仅管理员；读取任意登录用户。"""
        if self.action in {'create', 'update', 'partial_update', 'destroy', 'sync'}:
            return [permissions.IsAdminUser()]
        return [permissions.IsAuthenticated()]
    @action(detail=True, methods=['post'], url_path='sync')
    def sync(self, request, pk=None):
        """立即从 gm 拉取该账户的资金与持仓并回写。

        **在独立子进程中执行**：实测 gm SDK 在终端不可达时 ``get_cash()``
        会持有 GIL 无限阻塞，把整个 Web 进程冻住（整站打不开）。同进程内的
        任何超时方案都救不回来——主线程根本执行不到超时逻辑。故必须用子进程
        隔离，卡住时 ``kill()`` 即可，Web 进程不受影响。
        详见 :func:`run_account_sync_in_subprocess`。

        失败一律转成可读的 4xx（gm 不可用/终端无响应 400、回读繁忙 409），
        不让 500 冒到前端——一次同步失败不应表现为整站故障。
        """
        from django.db import OperationalError

        account = self.get_object()
        masked = mask_account(account.account_id)
        # 起止日志：访问日志只在**响应完成**时记录，一旦子进程卡住，
        # 整条请求不会在日志里留下痕迹（曾因此无法判断同步是否真的进来过）。
        logger.info('开始同步账户 %s（资金口径 %s）', masked, account.capital_basis)
        started = time.monotonic()

        # 先确认通道可用（缺令牌等快速失败，不必白起一个子进程）
        if not (getattr(django_settings, 'GM_TOKEN', '') or os.getenv('GM_TOKEN', '')):
            return Response(
                {'detail': '未配置 GM_TOKEN，无法连接 gm 终端'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        outcome = run_account_sync_in_subprocess(
            account.account_id, capital_basis=account.capital_basis)
        if not outcome['ok']:
            logger.info('账户 %s 同步未成功（耗时 %.1fs）：%s',
                        masked, time.monotonic() - started, outcome['detail'])
            return Response({'detail': outcome['detail']},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            account.refresh_from_db()
        except OperationalError:
            logger.warning('账户 %s 回读同步结果遇到写锁竞争', masked, exc_info=True)
            return Response({'detail': '同步已完成，但结果回读繁忙，请稍后刷新'},
                            status=status.HTTP_409_CONFLICT)
        logger.info('账户 %s 同步完成，耗时 %.2fs', masked, time.monotonic() - started)
        return Response({'account': AccountFundConfigSerializer(account).data})


class FundAllocationViewSet(viewsets.ModelViewSet):
    """分级资金申请 CRUD（Plan 占用 → Suite 申请 → Case 申请）。"""
    queryset = FundAllocation.objects.select_related(
        'plan', 'suite', 'case',
    ).all().order_by('-created_at')
    serializer_class = FundAllocationSerializer
    filterset_fields = ['plan', 'suite', 'case', 'level', 'status']


class OrderViewSet(viewsets.ModelViewSet):
    """委托单 CRUD"""
    queryset = Order.objects.select_related('log').all().order_by('-created_at')
    serializer_class = OrderSerializer
    filterset_fields = ['symbol', 'status']


class AlertChannelViewSet(viewsets.ModelViewSet):
    """告警渠道配置管理（管理员功能：渠道含收件人邮箱等 PII，见 N-05 权限分级）"""
    queryset = AlertChannel.objects.all()
    serializer_class = AlertChannelSerializer
    permission_classes = [permissions.IsAdminUser]
    filterset_fields = ['channel_type', 'is_enabled', 'min_severity']

    def perform_update(self, serializer):
        super().perform_update(serializer)
        # 重新加载告警渠道配置
        from .alerts import alert_service
        alert_service.reload_channels()

    @action(detail=False, methods=['post'], url_path='reload')
    def reload(self, request):
        """重新加载告警渠道配置"""
        from .alerts import alert_service
        alert_service.reload_channels()
        return Response({'status': 'ok', 'message': '告警渠道配置已重新加载'})


class AlertViewSet(viewsets.ReadOnlyModelViewSet):
    """告警查询视图（只读）"""
    queryset = Alert.objects.select_related('plan', 'suite_run', 'acknowledged_by', 'resolved_by').all().order_by('-created_at')
    serializer_class = AlertSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['alert_type', 'severity', 'status', 'in_app_notified', 'email_notified', 'plan']
    search_fields = ['title', 'message', 'error_code']

    @action(detail=True, methods=['post'], url_path='actions')
    def perform_action(self, request, pk=None):
        """执行告警操作（确认/解决）"""
        alert = self.get_object()
        serializer = AlertActionSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        alert_action = serializer.validated_data['action']
        note = serializer.validated_data.get('note', '')

        from django.utils import timezone

        if alert_action == 'acknowledge':
            if alert.status != 'pending':
                return Response(
                    {'detail': '只能确认待处理的告警'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            alert.status = 'acknowledged'
            alert.acknowledged_by = request.user
            alert.acknowledged_at = timezone.now()
            alert.save()
            return Response({'status': 'ok', 'message': '告警已确认'})

        if alert_action == 'resolve':
            if alert.status not in ['pending', 'acknowledged']:
                return Response(
                    {'detail': '只能确认待处理或已确认的告警'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            alert.status = 'resolved'
            alert.resolved_by = request.user
            alert.resolved_at = timezone.now()
            alert.message += f'\n\n解决备注: {note}' if note else alert.message
            alert.save()
            return Response({'status': 'ok', 'message': '告警已解决'})

        return Response({'detail': '无效的操作'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='statistics')
    def statistics(self, request):
        """获取告警统计信息"""
        from django.db.models import Count, Q

        stats = Alert.objects.aggregate(
            total=Count('id'),
            pending=Count('id', filter=Q(status='pending')),
            acknowledged=Count('id', filter=Q(status='acknowledged')),
            resolved=Count('id', filter=Q(status='resolved')),
            high_severity=Count('id', filter=Q(severity='high')),
            critical_severity=Count('id', filter=Q(severity='critical')),
        )

        # 按类型统计
        by_type = Alert.objects.values('alert_type').annotate(
            count=Count('id')
        ).order_by('-count')

        return Response({
            'overview': stats,
            'by_type': list(by_type),
        })

    @action(detail=True, methods=['post'], url_path='resend-notifications')
    def resend_notifications(self, request, pk=None):
        """重新发送告警通知"""
        alert = self.get_object()

        # 重置通知状态
        alert.in_app_notified = False
        alert.email_notified = False
        alert.notification_error = ''
        alert.save()

        # 重新发送通知
        from .alerts import alert_service
        try:
            alert_service.send_alert_notifications(alert)
            return Response({'status': 'ok', 'message': '告警通知已重新发送'})
        except Exception as e:
            return Response(
                {'detail': f'发送通知失败: {redact_text(str(e))}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
