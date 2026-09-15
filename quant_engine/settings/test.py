# 测试专用 settings：仅用于 `manage.py test`（manage.py 检测 test 子命令后切换）。
# 继承 dev 的 SQLite 双库与日志配置。
from .dev import *  # noqa: F401,F403  pylint: disable=w0401,w0614,c0413

# 关闭 arcis 反滥用中间件的请求限流。
# 原因：ArcisMiddleware 默认按客户端 IP 100 次/60 秒限流，测试进程内所有
# Django test client 请求共享 127.0.0.1，watchlists/datasources 等套件请求数
# 超过阈值后返回 429，导致业务断言被限流响应污染（非业务缺陷）。
# 生产/开发环境限流保持不变（见 base.py MIDDLEWARE 与 ARCIS_CONFIG）。
ARCIS_CONFIG = {'rate_limit': False}
