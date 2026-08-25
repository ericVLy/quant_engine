from rest_framework import viewsets, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from datetime import datetime, timedelta

from .models import DataSource, RealtimeSnapshot, KLineSyncLog
from .serializers import (
    DataSourceSerializer, RealtimeSnapshotSerializer,
    KLineSyncLogSerializer, KLineSerializer
)
from .services import sync_kline_for_symbol, sync_all_symbols
from apps.watchlists.models import Symbol


class DataSourceViewSet(viewsets.ModelViewSet):
    """数据源配置 CRUD"""
    queryset = DataSource.objects.all()
    serializer_class = DataSourceSerializer


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
        from .services import get_kline_model
        KLineModel = get_kline_model(symbol)

        try:
            start = datetime.strptime(start_date, '%Y-%m-%d').date()
            end = datetime.strptime(end_date, '%Y-%m-%d').date()
        except ValueError:
            raise serializers.ValidationError({"detail": "日期格式应为 YYYY-MM-DD"})

        qs = KLineModel.objects.filter(
            symbol=symbol,
            date__range=[start, end]
        ).order_by('date')

        results = []
        for item in qs:
            data = {
                'symbol': symbol.code,
                'date': item.date,
                'open': item.open,
                'high': item.high,
                'low': item.low,
                'close': item.close,
                'volume': item.volume,
                'amount': item.amount,
                'extra': {}
            }
            # 市场特定信息放入 extra
            if symbol.market == 'A':
                data['extra']['adj_factor'] = item.adj_factor
                data['extra']['turnover_rate'] = item.turnover_rate
            elif symbol.market == 'HK':
                data['extra']['prev_close'] = item.prev_close
                data['extra']['currency'] = item.currency
            elif symbol.market == 'US':
                data['extra']['split_factor'] = item.split_factor
            results.append(data)

        serializer = self.get_serializer(results, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='sync')
    def sync_kline(self, request):
        """触发K线同步：POST 传入 symbol_code (或 'all'), start_date, end_date, adjust"""
        symbol_code = request.data.get('symbol')
        sync_type = request.data.get('sync_type', 'daily')
        start_date = request.data.get('start_date')
        end_date = request.data.get('end_date')
        adjust = request.data.get('adjust', 'qfq')

        # 默认日期范围：最近30天
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        if not end_date:
            end_date = datetime.now().strftime('%Y-%m-%d')

        if symbol_code == 'all':
            results = sync_all_symbols(sync_type, start_date, end_date, adjust)
            return Response({'status': 'completed', 'results': results})
        else:
            symbol = get_object_or_404(Symbol, code=symbol_code)
            added, skipped, error = sync_kline_for_symbol(
                symbol, sync_type,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust
            )
            KLineSyncLog.objects.create(
                symbol=symbol,
                sync_type=sync_type,
                start_date=start_date,
                end_date=end_date,
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