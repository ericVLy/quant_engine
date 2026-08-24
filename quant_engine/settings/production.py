# pylint: disable=C0413,C0411,w0401,w0614
import os
from .base import *

DEBUG = False



# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


ALLOWED_HOSTS = ["*"]

try:
    from .local import *
except ImportError:
    import secrets

    SECRET_KEY = secrets.token_urlsafe(32)

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
