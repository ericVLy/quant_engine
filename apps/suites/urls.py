from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import SuiteViewSet

router = DefaultRouter()
router.register(r'', SuiteViewSet, basename='suites')

app_name = 'suites'
urlpatterns = [path('', include(router.urls))]
