"""Windows 单机生产配置（2026-10-14）。

与 :mod:`quant_engine.settings.production` 的差异：**强制使用 MariaDB**，
不再沿用 ``local.py`` 里的 SQLite 开发配置。

为什么不直接改 ``local.py``：
``dev.py`` 与 ``production.py`` 都 ``from .local import *``。改 ``local.py``
会把**开发环境**也一并切到 MariaDB，日常开发（跑测试、外发行情、动态建分表）
都会受影响。故单独建本模块，只在 Windows 生产部署时显式指定。

用法（启动脚本里设置）::

    DJANGO_SETTINGS_MODULE=quant_engine.settings.win_production
    DB_NAME=quant_engine
    DB_USER=quant
    DB_PASSWORD=***
    KLINE_DB_NAME=quant_kline
    ...

MariaDB 服务本身需已启动（本机为 ``MariaDB`` 服务，StartType=Automatic）。

驱动：Windows 下用 ``pymysql`` 伪装成 ``MySQLdb``（``mysqlclient`` 无Windows
轮子），必须在 Django 初始化前调用 ``install_as_MySQLdb()``。
"""
# pylint: disable=wildcard-import,unused-wildcard-import,w0401  # 需继承 production 的全部配置
import os

from .production import *  # noqa: F401,F403  # pylint: disable=redefined-builtin
from .base import KLINE_DB_ALIAS

import pymysql

pymysql.install_as_MySQLdb()


def _db(name_env, user_env, password_env, host_env, port_env, default_name):
    """从环境变量拼装一个 MariaDB 库配置（凭据不落文件）。"""
    return {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv(name_env, default_name),
        'USER': os.getenv(user_env, 'quant'),
        'PASSWORD': os.getenv(password_env, ''),
        'HOST': os.getenv(host_env, '127.0.0.1'),
        'PORT': os.getenv(port_env, '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            'sql_mode': 'STRICT_TRANS_TABLES',
        },
        'TEST': {
            'NAME': os.getenv(f'TEST_{name_env}', f'test_{default_name}'),
        },
    }


DATABASES = {
    'default': _db('DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT', 'quant_engine'),
    KLINE_DB_ALIAS: _db('KLINE_DB_NAME', 'KLINE_DB_USER', 'KLINE_DB_PASSWORD',
                        'KLINE_DB_HOST', 'KLINE_DB_PORT', 'quant_kline'),
}

# MariaDB 的 InnoDB 行锁替代了 SQLite 的库级写锁：不再需要 transaction_mode，
# 多 worker 并发写也不会出现「一个请求占锁、其余阻塞至超时」。
DATABASES['default']['CONN_MAX_AGE'] = int(os.getenv('DB_CONN_MAX_AGE', '60'))
DATABASES[KLINE_DB_ALIAS]['CONN_MAX_AGE'] = int(os.getenv('KLINE_DB_CONN_MAX_AGE', '60'))

# 单机部署不对外暴露 Host（waitress 仅监听 127.0.0.1，由 nginx 或用户直连访问）
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if h.strip()]

# ---------------------------------------------------------------------------
# 置于 nginx 反向代理之后所需的设置
#
# nginx 会把原始 Host / 协议放在 X-Forwarded-* 里。若不信任这些头：
#   - Django 看到的 Host 是 127.0.0.1:8000，与浏览器访问的域名不一致；
#   - 部署在 HTTPS 反代后时，Django 会认定请求非安全，CSRF 判定随之出错。
# 仅在确认上游只有本机 nginx（默认部署形态）时才可开启。
# ---------------------------------------------------------------------------
TRUST_PROXY_HEADERS = os.getenv('TRUST_PROXY_HEADERS', '1').lower() in {'1', 'true', 'yes'}
if TRUST_PROXY_HEADERS:
    USE_X_FORWARDED_HOST = True
    USE_X_FORWARDED_PORT = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# 通过域名或局域网 IP 访问时，POST 的 CSRF 校验需要显式信任该 Origin，
# 否则浏览器发来的 Origin 会被判为跨站而拒绝。非 HTTPS 部署必须配置这一项。
# 例：CSRF_TRUSTED_ORIGINS=http://192.168.1.20:8080,http://quantbox.local:8080
CSRF_TRUSTED_ORIGINS = [
    o.strip().rstrip('/') for o in
    os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# 完成初始化后应置 SETUP_ENABLED=0，使 /setup 端点在代码层不可达（见 README 部署引导）
SETUP_ENABLED = os.getenv('SETUP_ENABLED', '1').lower() in {'1', 'true', 'yes'}

# 采集与告警等外部访问在生产默认开启（可用环境变量关闭）
FUNDAMENTALS_ENABLED = os.getenv('FUNDAMENTALS_ENABLED', '1').lower() in {'1', 'true', 'yes'}
MONITORING_UPDATER_ENABLED = os.getenv('MONITORING_UPDATER_ENABLED', '1').lower() in {'1', 'true', 'yes'}
