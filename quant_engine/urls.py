"""
URL configuration for quant_engine project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from pathlib import Path

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import include, path, re_path


def serve_frontend_index(request):
    index_path = Path(settings.BASE_DIR) / 'static' / 'index.html'
    if not index_path.exists():
        raise Http404('Frontend build not found.')
    return FileResponse(index_path.open('rb'), content_type='text/html')


urlpatterns = [
    path('', serve_frontend_index, name='frontend-root'),
    path('admin/', admin.site.urls),
    path('api/datasources/', include('apps.datasources.urls')),
    path('api/watchlists/', include('apps.watchlists.urls')),
    path('api/execution/', include('apps.execution.urls')),
    path('api/cases/', include('apps.cases.urls')),
    path('api/suites/', include('apps.suites.urls')),
    path('api/plans/', include('apps.plans.urls')),
    path('api/users/', include('apps.users.urls')),
    re_path(r'^(?!api/|admin/|assets/).*$', serve_frontend_index),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=str(settings.STATIC_ROOT))

