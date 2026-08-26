from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Edge, Suite
from .serializers import EdgeSerializer, SuiteSerializer
from .services import SuiteError, publish_suite, update_topology


class SuiteViewSet(viewsets.ModelViewSet):
    queryset = Suite.objects.prefetch_related('cases', 'out_edges', 'children').all()
    serializer_class = SuiteSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        for field in ('status', 'aggregate_method'):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def destroy(self, request, *args, **kwargs):
        suite = self.get_object()
        if suite.plans.exists():
            return Response(
                {'detail': 'Suite 已被 Plan 引用，不能删除'},
                status=status.HTTP_409_CONFLICT,
            )
        suite.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get', 'post'])
    def topology(self, request, pk=None):
        suite = self.get_object()
        if request.method == 'POST':
            try:
                update_topology(
                    suite,
                    request.data.get('case_ids', []),
                    request.data.get('edges', []),
                )
            except (SuiteError, TypeError, AttributeError) as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'suite': self.get_serializer(suite).data,
            'cases': [
                {'id': case.id, 'name': case.name, 'node_type': case.node_type,
                 'status': case.status, 'params': case.params}
                for case in suite.cases.all()
            ],
            'edges': EdgeSerializer(
                Edge.objects.filter(from_suite=suite), many=True
            ).data,
        })

    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        suite = self.get_object()
        try:
            publish_suite(suite)
        except SuiteError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(suite).data)
