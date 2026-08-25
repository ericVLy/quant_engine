from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'event-types', views.EventTypeRegistryViewSet, basename='event-types')
router.register(r'runs', views.SuiteRunViewSet, basename='runs')
router.register(r'events', views.EventViewSet, basename='events')
router.register(r'logs', views.ExecutionLogViewSet, basename='logs')
router.register(r'orders', views.OrderViewSet, basename='orders')

urlpatterns = [
    path('', include(router.urls)),
    # 占位触发器（待集成 runner）
    path('trigger/', views.trigger_plan, name='trigger'),
    path('run/<int:run_id>/', views.suite_run_status, name='run-status'),
    path('run/<int:run_id>/start/', views.start_run, name='run-start'),
    path('run/<int:run_id>/process/', views.process_run_event, name='run-process'),
]