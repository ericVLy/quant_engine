# pylint: disable=C0413,C0411,w0401,w0614
import os
from .base import *

try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover - optional for SQLite/dev environments
    pass

DEBUG = False



# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


ALLOWED_HOSTS = ["*"]


try:
    from .local import *
except ImportError:
    import secrets

    SECRET_KEY = secrets.token_urlsafe(32)

    # Database (production default: SQLite for main DB, optional MariaDB for K-line DB)
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        },
        KLINE_DB_ALIAS: {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'kline.sqlite3',
        },
    }

    if os.getenv('USE_KLINE_MARIADB', '0').lower() in {'1', 'true', 'yes'}:
        DATABASES[KLINE_DB_ALIAS] = {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': os.getenv('KLINE_DB_NAME', 'quant_kline'),
            'USER': os.getenv('KLINE_DB_USER', 'root'),
            'PASSWORD': os.getenv('KLINE_DB_PASSWORD', ''),
            'HOST': os.getenv('KLINE_DB_HOST', '127.0.0.1'),
            'PORT': os.getenv('KLINE_DB_PORT', '3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
                'sql_mode': 'STRICT_TRANS_TABLES',
            },
            'TEST': {
                'NAME': os.getenv('KLINE_DB_TEST_NAME', 'test_quant_kline'),
            },
        }

__base_path__ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__log_path__ = os.path.join(__base_path__, "logs")

if not os.path.exists(__log_path__):
    os.makedirs(__log_path__)
from datetime import datetime

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{levelname}][{asctime}][{module}][{process:d}][{thread:d}][{message}]",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "file": {
            'formatter': 'verbose',
            "level": "INFO",
            "class": "logging.FileHandler",
            "filename": os.path.join(__log_path__, 
                                        f"django_logfile_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')[:23]}.log"),
        },
    },
    "loggers": {
        "django": {
            "handlers": ["file"],
            "level": "INFO",
            "propagate": True,
        },
    },
}
