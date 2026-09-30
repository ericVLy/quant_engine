"""运行总览路由。"""
from django.urls import path

from . import views

urlpatterns = [
    path("overview/", views.OverviewView.as_view(), name="dashboard-overview"),
    path("execution-trend/", views.ExecutionTrendView.as_view(),
         name="dashboard-execution-trend"),
]
