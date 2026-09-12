from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import IntradayViewSet

router = DefaultRouter()
router.register(r'intraday', IntradayViewSet, basename='intraday')

urlpatterns = [
    path('', include(router.urls)),
]