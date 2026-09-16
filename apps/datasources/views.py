from rest_framework import viewsets, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from datetime import datetime, timedelta

from .models import RealtimeSnapshot, KLineSyncLog
from .serializers import (
    RealtimeSnapshotSerializer,
    KLineSyncLogSerializer, KLineSerializer
)
from .services import sync_kline_for_symbol, sync_all_symbols
from apps.watchlists.models import Symbol


class RealtimeSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """实时快照只读（更新由外部数据推送服务完成）"""
    queryset = RealtimeSnapshot.objects.select_related('symbol').all()
    serializer_class = RealtimeSnapshotSerializer
    lookup_field = 'symbol_id'

    def get_object(self):
        symbol_id = self.kwargs.get('symbol_id')
        return get_object_or_404(RealtimeSnapshot, symbol_id=symbol_id)


class KLineSyncLogViewSet(viewsets.ReadOnlyModelViewSet):
    """同步日志只读"""
    queryset = KLineSyncLog.objects.select_related('symbol').all().order_by('-created_at')
    serializer_class = KLineSyncLogSerializer
    # 可添加 filterset_fields


class KLineViewSet(viewsets.GenericViewSet):
    """K线数据查询和同步触发"""
    serializer_class = KLineSerializer

    @action(detail=False, methods=['get'], url_path='query')
    def query_kline(self, request):
        """查询K线数据：?symbol=xxx&start=YYYY-MM-DD&end=YYYY-MM-DD"""
        symbol_code = request.query_params.get('symbol')
        start_date = request.query_params.get('start')
        end_date = request.query_params.get('end')

        if not symbol_code:
            raise serializers.ValidationError({"detail": "symbol 参数必填"})
        if not start_date or not end_date:
            raise serializers.ValidationError({"detail": "start 和 end 日期必填"})

        symbol = get_object_or_404(Symbol, code=symbol_code)
        from .services import query_kline_table

        try:
            start = datetime.strptime(start_date, '%Y-%m-%d').date()
            end = datetime.strptime(end_date, '%Y-%m-%d').date()
        except ValueError:
            raise serializers.ValidationError({"detail": "日期格式应为 YYYY-MM-DD"})

        results = query_kline_table(symbol, start, end)
        page = self.paginate_queryset(results)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(results, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='sync')
    def sync_kline(self, request):
        """触发K线同步：POST 传入 symbol_code (或 'all'), start_date, end_date, adjust。

        未提供 start_date 时按增量语义处理（服务端自动从库内最新日期之后拉取）。
        """
        symbol_code = request.data.get('symbol')
        sync_type = request.data.get('sync_type', 'daily')
        start_date = request.data.get('start_date')
        end_date = request.data.get('end_date')
        adjust = request.data.get('adjust', 'qfq')

        # 请求窗口（用于日志记录）：缺省最近30天
        log_start = start_date or (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        log_end = end_date or datetime.now().strftime('%Y-%m-%d')
        # 未显式提供 start_date 时传 None，让服务端走增量判断（从库内最新日期之后拉取）
        service_start = start_date or None

        if symbol_code == 'all':
            results = sync_all_symbols(sync_type, service_start, end_date or None, adjust)
            return Response({'status': 'completed', 'results': results})
        else:
            symbol = get_object_or_404(Symbol, code=symbol_code)
            added, skipped, error = sync_kline_for_symbol(
                symbol, sync_type,
                start_date=service_start,
                end_date=end_date,
                adjust=adjust
            )
            KLineSyncLog.objects.create(
                symbol=symbol,
                sync_type=sync_type,
                start_date=log_start,
                end_date=log_end,
                records_added=added,
                records_skipped=skipped,
                status='success' if error is None else 'failed',
                error_msg=error or ''
            )
            return Response({
                'symbol': symbol.code,
                'added': added,
                'skipped': skipped,
                'error': error
            })