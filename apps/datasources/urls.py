from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    RealtimeSnapshotViewSet,
    KLineSyncLogViewSet, KLineViewSet
)

router = DefaultRouter()
router.register(r'snapshots', RealtimeSnapshotViewSet, basename='snapshot')
router.register(r'sync-logs', KLineSyncLogViewSet, basename='synclog')
# KLineViewSet 自定义，不使用默认的 basename，单独注册
router.register(r'kline', KLineViewSet, basename='kline')

urlpatterns = [
    path('', include(router.urls)),
]