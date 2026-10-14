# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from django.apps import AppConfig


from django.conf import settings


class MonitoringConfig(AppConfig):
    name = 'apps.monitoring'
    verbose_name = '分时监控'

    def ready(self):
        """随 Django 服务启动分时数据更新器（**采样在独立子进程中运行**）。

        - ``MONITORING_UPDATER_ENABLED=False``（或环境变量置 0）时不启动；
        - **非 Web 服务进程一律不启动**（见下方 ``skip_commands``）：策略调度器
          （``run_scheduler``）、开发栈父进程（``run_dev_stack``）、MCP 服务
          （``run_mcp_server`` / ``python -m mcp_server``）、交互/维护命令
          （``gm_live_link`` / ``clear_intraday`` / ``purge_execution_logs``）
          以及**采样子进程自身**（``run_intraday_updater``，避免递归拉起）。
          分时采样与写库只由 Django 服务进程负责，避免多进程重复外部请求；
        - ``runserver`` 自动重载父进程不启动（仅 ``RUN_MAIN=true`` 的子进程启动）。

        **为什么采样放子进程**（2026-10-14）：采样经 ``GmSnapshotProvider`` 调用
        gm SDK 的 ``api.history``，实测在对端"可连接但不响应"时该调用**持有 GIL
        一直阻塞**，同进程内的心跳线程与 asyncio 循环全部停摆，
        ``wait_for`` / ``join(timeout)`` 的超时逻辑根本执行不到——整个 Web 进程
        会被冻结（整站打不开）。子进程卡死时父进程可 ``kill()`` 并重启，
        Web 服务不受影响。详见 ``updater.SubprocessIntradayUpdater``。

        Note:
            Gunicorn / WSGI 等生产服务进程**仍会启动**更新器（argv 既不在
            ``skip_commands`` 也不是 ``runserver``）。若用 ``--workers N``
            多进程部署，每个 worker 都会拉起一个采样子进程（重复采样），
            应改为单进程或用 ``MONITORING_UPDATER_ENABLED=0`` 只在一个 worker 启用。
        """
        import os
        import sys

        if not getattr(settings, 'MONITORING_UPDATER_ENABLED', False):
            return
        skip_commands = {
            'test', 'migrate', 'makemigrations', 'collectstatic', 'shell', 'check',
            'createsuperuser',
            # 非 Web 服务的长驻 / 运维进程：禁止在此采样分时数据
            'run_mcp_server', 'run_scheduler', 'run_dev_stack', 'gm_live_link',
            'clear_intraday', 'purge_execution_logs',
            # 采样子进程自身：它就是采样器，再拉一个会无限递归
            'run_intraday_updater',
        }
        if skip_commands.intersection(sys.argv[1:]):
            return
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return

        from .updater import SubprocessIntradayUpdater

        SubprocessIntradayUpdater().start()
