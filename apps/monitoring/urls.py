from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views
from .views import IntradayViewSet

router = DefaultRouter()
router.register(r'intraday', IntradayViewSet, basename='intraday')

urlpatterns = [
    # SSE 持久化推送：普通 Django 视图（不走 DRF 内容协商，避免 EventSource 406）
    path('intraday/stream/', views.intraday_stream),
    path('', include(router.urls)),
]