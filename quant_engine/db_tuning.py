"""SQLite 连接调优（多进程开发栈并发友好）。

`run_dev_stack` 下 Django 服务与 MCP 服务两个进程共享同一个 SQLite 文件；
默认 journal=delete 模式下写锁互斥，更新器启动回填与 MCP 进程初始化并发时
极易触发 ``database is locked``。

通过 ``connection_created`` 信号为每个 SQLite 连接执行：
- ``PRAGMA journal_mode=WAL;``   —— 读写并发（读不阻塞写）；
- ``PRAGMA busy_timeout=5000;``  —— 写锁冲突时等待重试而非立即报错。

仅对 SQLite（vendor='sqlite'）生效；生产 MariaDB 不受影响。
"""

from django.db.backends.signals import connection_created


def configure_sqlite_pragmas(sender, connection, **kwargs):  # noqa: ANN001
    if connection.vendor != 'sqlite':
        return
    # busy_timeout 与 OPTIONS['timeout']（秒）保持一致，避免覆盖 sqlite3.connect
    # 已设置的等待时长（Django 默认 5s；开发栈配置 30s）。
    timeout_seconds = connection.settings_dict.get('OPTIONS', {}).get('timeout', 5)
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA busy_timeout=%d;' % int(float(timeout_seconds) * 1000))


connection_created.connect(configure_sqlite_pragmas, dispatch_uid='sqlite_pragmas')
