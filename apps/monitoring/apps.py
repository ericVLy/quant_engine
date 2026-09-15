from django.apps import AppConfig


from django.conf import settings


class MonitoringConfig(AppConfig):
    name = 'apps.monitoring'
    verbose_name = '分时监控'

    def ready(self):
        """随 Django 服务启动内部分时数据更新器（禁止单独更新命令）。

        - ``MONITORING_UPDATER_ENABLED=False``（或环境变量置 0）时不启动；
        - ``test`` / ``migrate`` / ``makemigrations`` / ``collectstatic`` /
          ``shell`` / ``check`` 等管理命令进程不启动；
        - MCP 服务进程（``run_mcp_server`` / ``python -m mcp_server``）不启动：
          分时更新由 Django 服务进程负责，避免多进程重复采样（外部数据源请求 + 库写入）；
        - ``runserver`` 自动重载父进程不启动（仅 ``RUN_MAIN=true`` 的子进程启动）。
        """
        import os
        import sys

        if not getattr(settings, 'MONITORING_UPDATER_ENABLED', False):
            return
        skip_commands = {
            'test', 'migrate', 'makemigrations', 'collectstatic', 'shell', 'check',
            'createsuperuser', 'run_mcp_server',
        }
        if skip_commands.intersection(sys.argv[1:]):
            return
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return

        from .updater import get_updater

        get_updater().start()
