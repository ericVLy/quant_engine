# pylint: disable=C0413,C0411,w0401,w0614
import os
from .base import *

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True


# SECURITY WARNING: define the correct hosts in production!
ALLOWED_HOSTS = ["*"]



# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


try:
    from .local import *
except ImportError:
    GM_TOKEN = os.getenv('GM_TOKEN', '')
    # 远程掘金终端服务地址（如 "192.168.1.10:7001"）；空 = 本机终端（SDK 缺省）。
    GM_SERV_ADDR = os.getenv('GM_SERV_ADDR', '')
    # SECURITY WARNING: keep the secret key used in production secret!
    SECRET_KEY = 'django-insecure-7ya^@-)^rrgxn!!)r(r)#^eo^zu3d_#r$0ibpyv@$_a$nmvgdp'
    # Database (development default: SQLite for both main app DB and K-line DB)
    # SQLite 多进程写并发调优（见 DB_TUNING / tests_db_tuning）：
    # - transaction_mode=IMMEDIATE：写事务提前抢占，避免升级死锁（Django 5.1+）
    # - WAL 持久化由 init 脚本/连接设置（tests_db_tuning 验证）
    # - timeout=30：busy 等待 30s，吸收 web/mcp 两个进程 updater 同时写主库的竞争
    _SQLITE_OPTIONS = {'transaction_mode': 'IMMEDIATE', 'timeout': 30}
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': dict(_SQLITE_OPTIONS),
        },
        KLINE_DB_ALIAS: {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'kline.sqlite3',
            'OPTIONS': dict(_SQLITE_OPTIONS),
            # 与主库相同：多进程写并发启用 WAL，消除 "database is locked"（见 DB_TUNING）
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
        "console": {
            "level": "INFO",
            "filters": ["redaction"],
            "class": "logging.StreamHandler",
            "formatter": "verbose"
        }
    },
    "loggers": {
        "django": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps.datasources.tests": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps.datasources.services": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps.watchlists.tests": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps.execution.tests": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps.cases.tests": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps.suites.tests": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps.plans.tests": {
            "handlers": ["file", "console"],
            "level": "INFO",
            "propagate": True,
        },
    },
}
