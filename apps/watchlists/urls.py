from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SymbolViewSet, GroupViewSet, WatchlistViewSet

router = DefaultRouter()
router.register(r'symbols', SymbolViewSet, basename='symbol')
router.register(r'groups', GroupViewSet, basename='group')

urlpatterns = [
    path('', include(router.urls)),
    path('watchlist/', WatchlistViewSet.as_view({
        'get': 'list',
        'put': 'update',
        'patch': 'partial_update',
        'post': 'create',
    })),
]