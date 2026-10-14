"""在**独立进程**中运行分时数据更新器（由 Web 进程以子进程方式拉起）。

为什么需要独立进程：
分时采样经 ``GmSnapshotProvider`` 调用 gm SDK 的 ``api.history``。实测该调用在
对端"可连接但不响应"时**持有 GIL 一直阻塞**——同进程内的心跳线程与 asyncio
事件循环全部停摆，``wait_for`` / ``join(timeout)`` 的超时逻辑根本执行不到，
**整个 Web 进程被冻结**（浏览器一直转圈、整站打不开）。

把采样放进子进程后，卡死只影响本进程：父进程（见
``apps.monitoring.updater.SubprocessIntradayUpdater``）能 ``kill()`` 并重启，
Web 服务不受任何影响。

本命令通常**不需要手工运行**——``MonitoringConfig.ready()`` 会自动拉起它。
需要前台调试时可直接执行：

    .venv/bin/python manage.py run_intraday_updater
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避加载期副作用
import os
import signal

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '在前台进程运行分时数据更新器（通常由 Web 进程以子进程方式拉起）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval', dest='interval', type=int, default=0,
            help='采样间隔秒数；留空=用settings.MONITORING_UPDATER_INTERVAL。',
        )

    def handle(self, *args, **options):
        # 本进程自己就是采样进程，必须关掉内置更新器，
        # 否则 AppConfig.ready() 会再拉一个子进程，造成递归。
        os.environ['MONITORING_UPDATER_ENABLED'] = '0'

        from apps.monitoring.updater import IntradayUpdater

        interval = options.get('interval') or None
        updater = IntradayUpdater(interval=interval)

        def _shutdown(signum, _frame):  # pragma: no cover - 信号路径
            self.stdout.write(f'收到信号 {signum}，停止更新器…')
            updater._stop_event.set()  # pylint: disable=protected-access

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _shutdown)
            except (ValueError, OSError):  # 非主线程 / 不支持该平台
                pass

        updater.start()
        self.stdout.write(
            f'[monitoring-updater] 采样进程已启动（间隔 {updater.interval}s），'
            '心跳文件：'
            f'{updater.heartbeat_path}'
        )
        # 前台阻塞：把主线程交给更新器的守护逻辑
        updater._thread.join()  # pylint: disable=protected-access
        self.stdout.write('[monitoring-updater] 采样进程退出')
