from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Case, CaseVersion
from .serializers import CaseVersionSerializer
from .serializers import CaseSerializer
from .services import CaseError, delete_case, publish_case


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
        try:
            delete_case(self.get_object())
        except CaseError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        case = self.get_object()
        publish_case(case)
        return Response(self.get_serializer(case).data)

    @action(detail=True, methods=['get'])
    def versions(self, request, pk=None):
        case = self.get_object()
        versions = CaseVersion.objects.filter(case=case).order_by('-version')
        page = self.paginate_queryset(versions)
        if page is not None:
            return self.get_paginated_response(
                CaseVersionSerializer(page, many=True).data
            )
        return Response(CaseVersionSerializer(versions, many=True).data)
