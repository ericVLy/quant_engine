from django.urls import path
from . import views

app_name = 'datasources'
urlpatterns = [    path('', views.list_create, name='list-create'),
    path('<int:pk>/', views.detail, name='detail'),
]
