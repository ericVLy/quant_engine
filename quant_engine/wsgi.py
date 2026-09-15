"""
WSGI config for quant_engine project.

It exposes the WSGI callable as a module-level variable named ``application``.

生产部署（Gunicorn）::

    gunicorn quant_engine.wsgi:application --workers 4 --bind 0.0.0.0:8000 \\
        --env DJANGO_SETTINGS_MODULE=quant_engine.settings.production

- 默认使用 production settings（数据库默认 MariaDB，见 production.py）；
  也可通过环境变量 DJANGO_SETTINGS_MODULE 显式覆盖（setdefault 语义不覆盖外部注入）。
- 开发 runserver 不经本入口（manage.py 默认 dev settings）。

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'quant_engine.settings.production')

application = get_wsgi_application()
