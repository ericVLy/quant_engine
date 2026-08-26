from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import PlanViewSet

router = DefaultRouter()
router.register(r'', PlanViewSet, basename='plans')

app_name = 'plans'
urlpatterns = [path('', include(router.urls))]
