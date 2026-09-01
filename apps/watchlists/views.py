import logging
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from .models import Symbol, Group, Watchlist
from .serializers import SymbolSerializer, GroupSerializer, WatchlistSerializer
from .services import sync_market_data, resolve_symbol_name

logger = logging.getLogger(__name__)


class SymbolViewSet(viewsets.ModelViewSet):
    queryset = Symbol.objects.all().order_by('code')
    serializer_class = SymbolSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    search_fields = ['code', 'name']
    filterset_fields = ['market', 'exchange']

    @action(detail=False, methods=['get'], url_path='resolve-name')
    def resolve_name(self, request):
        code = request.query_params.get('code', '').strip()
        market = request.query_params.get('market', '').strip() or None
        if not code:
            return Response({'detail': 'code 不能为空'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'name': resolve_symbol_name(code, market)})

    @action(detail=False, methods=['post'], url_path='sync')
    def sync_market(self, request):
        result = sync_market_data()
        return Response(result)

    @action(detail=False, methods=['post'], url_path='batch-import')
    def batch_import(self, request):
        data = request.data
        if not isinstance(data, list):
            return Response(
                {'error': '数据格式应为列表'},
                status=status.HTTP_400_BAD_REQUEST
            )
        created = []
        errors = []
        for item in data:
            code = item.get('code')
            name = item.get('name')
            market = item.get('market', 'A')
            exchange = item.get('exchange', '')
            if not code or not name:
                errors.append({'item': item, 'error': '缺少 code 或 name'})
                continue
            try:
                obj, is_created = Symbol.objects.update_or_create(
                    code=code,
                    defaults={
                        'name': name,
                        'market': market,
                        'exchange': exchange
                    }
                )
                if is_created:
                    created.append(obj.code)
            except Exception as e:
                errors.append({'item': item, 'error': str(e)})
        return Response({'created': created, 'errors': errors})


class GroupViewSet(viewsets.ModelViewSet):
    queryset = Group.objects.all().order_by('name')
    serializer_class = GroupSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']

    @action(detail=True, methods=['post'], url_path='add-symbols')
    def add_symbols(self, request, pk=None):
        group = self.get_object()
        symbol_ids = request.data.get('symbol_ids', [])
        if not symbol_ids:
            return Response(
                {'error': 'symbol_ids 不能为空'},
                status=status.HTTP_400_BAD_REQUEST
            )
        symbols = Symbol.objects.filter(id__in=symbol_ids)
        added = symbols.count()
        group.symbols.add(*symbols)
        return Response({'added': added})

    @action(detail=True, methods=['post'], url_path='remove-symbols')
    def remove_symbols(self, request, pk=None):
        group = self.get_object()
        symbol_ids = request.data.get('symbol_ids', [])
        if not symbol_ids:
            return Response(
                {'error': 'symbol_ids 不能为空'},
                status=status.HTTP_400_BAD_REQUEST
            )
        symbols = Symbol.objects.filter(id__in=symbol_ids)
        removed = symbols.count()
        group.symbols.remove(*symbols)
        return Response({'removed': removed})


class WatchlistViewSet(viewsets.GenericViewSet):
    """
    当前用户的 Watchlist（每个用户仅一个）
    手动路由，不包含 pk
    """
    permission_classes = [IsAuthenticated]
    serializer_class = WatchlistSerializer

    def get_object(self):
        obj, created = Watchlist.objects.get_or_create(user=self.request.user)
        return obj

    def list(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_update(self, serializer):
        serializer.save()

    def create(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        return Response(
            {'error': '不允许删除 Watchlist'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )