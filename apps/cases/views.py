from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Case
from .serializers import CaseSerializer


class CaseViewSet(viewsets.ModelViewSet):
    queryset = Case.objects.all().order_by('-updated_at')
    serializer_class = CaseSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search)
        for field in ('node_type', 'status'):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        return queryset

    def destroy(self, request, *args, **kwargs):
        case = self.get_object()
        if case.suites.exists():
            return Response(
                {'detail': 'Case 已被 Suite 引用，不能删除'},
                status=status.HTTP_409_CONFLICT,
            )
        case.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        case = self.get_object()
        case.status = 'published'
        case.version += 1
        case.save(update_fields=('status', 'version', 'updated_at'))
        return Response(self.get_serializer(case).data)
