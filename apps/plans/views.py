from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Plan
from .serializers import PlanSerializer
from .services import PlanError, delete_plan, publish_plan, resolve_plan_symbols


class PlanViewSet(viewsets.ModelViewSet):
    queryset = Plan.objects.select_related('root_suite').all().order_by('-updated_at')
    serializer_class = PlanSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        for field in ('status', 'trigger_type', 'exec_mode'):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def destroy(self, request, *args, **kwargs):
        try:
            delete_plan(self.get_object())
        except PlanError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        try:
            plan = publish_plan(self.get_object())
        except PlanError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(plan).data)

    @action(detail=True, methods=['get'])
    def symbols(self, request, pk=None):
        plan = self.get_object()
        return Response([
            {'id': symbol.id, 'code': symbol.code, 'name': symbol.name,
             'market': symbol.market, 'exchange': symbol.exchange}
            for symbol in resolve_plan_symbols(plan)
        ])
