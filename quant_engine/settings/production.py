# pylint: disable=C0413,C0411,w0401,w0614
import os
from .base import *

try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover - optional for SQLite/dev environments
    pass

DEBUG = False
FUNDAMENTALS_ENABLED = os.getenv('FUNDAMENTALS_ENABLED', '1').lower() in {'1', 'true', 'yes'}



# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


ALLOWED_HOSTS = ["*"]


try:
    from .local import *
except ImportError:
    import secrets
    GM_TOKEN = os.getenv('GM_TOKEN', '')
    # 远程掘金终端服务地址（如 "192.168.1.10:7001"）；空 = 本机终端（SDK 缺省）。
    GM_SERV_ADDR = os.getenv('GM_SERV_ADDR', '')

    # 预期行为（2026-09-15 确认）：SECRET_KEY 仅覆盖登录态（Session/CSRF），
    # 随机刷新仅导致用户重新登录；若需跨重启固定，经 local.py 或环境变量注入。
    SECRET_KEY = secrets.token_urlsafe(32)

    # 数据库（生产默认 MariaDB：主库与 K 线库；凭据仅经环境变量注入，不落文件。
    # USE_SQLITE=1 时回退 SQLite，仅限本机演练/CI，不作为生产形态）
    if os.getenv('USE_SQLITE', '0').lower() in {'1', 'true', 'yes'}:
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
    else:
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.mysql',
                'NAME': os.getenv('DB_NAME', 'quant'),
                'USER': os.getenv('DB_USER', 'quant'),
                'PASSWORD': os.getenv('DB_PASSWORD', ''),
                'HOST': os.getenv('DB_HOST', '127.0.0.1'),
                'PORT': os.getenv('DB_PORT', '3306'),
                'OPTIONS': {
                    'charset': 'utf8mb4',
                    'sql_mode': 'STRICT_TRANS_TABLES',
                },
                'TEST': {
                    'NAME': os.getenv('DB_TEST_NAME', 'test_quant'),
                },
            },
            KLINE_DB_ALIAS: {
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
    "filters": {
        "redaction": {"()": "apps.execution.redaction.RedactionLogFilter"},
    },
    "handlers": {
        "file": {
            'formatter': 'verbose',
            "level": "INFO",
            "filters": ["redaction"],
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
