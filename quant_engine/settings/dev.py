# pylint: disable=C0413,C0411,w0401,w0614
import os
from .base import *

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-7ya^@-)^rrgxn!!)r(r)#^eo^zu3d_#r$0ibpyv@$_a$nmvgdp'

# SECURITY WARNING: define the correct hosts in production!
ALLOWED_HOSTS = ["*"]

# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


try:
    from .local import *
except ImportError:
    pass


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
        "apps.datasources.tests": {
            "handlers": ["file"],
            "level": "INFO",
            "propagate": True,
        },
    },
}
