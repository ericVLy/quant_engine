from django.urls import path
from . import views

app_name = 'execution'
urlpatterns = [    path('trigger/', views.trigger_plan, name='trigger'),
    path('run/<int:run_id>/', views.suite_run_status, name='run-status'),
]
