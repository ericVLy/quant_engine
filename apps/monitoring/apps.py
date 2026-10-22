# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from django.apps import AppConfig


from django.conf import settings


class MonitoringConfig(AppConfig):
    name = 'apps.monitoring'
    verbose_name = '分时监控'

    def ready(self):
        """随 Django 服务启动分时数据更新器（**采样在独立子进程中运行**）。

        - ``MONITORING_UPDATER_ENABLED=False``（或环境变量置 0）时不启动；
        - **只有 Web 服务进程才启动**（白名单：``runserver`` / ``waitress`` /
          ``gunicorn`` / ``uvicorn``）。策略调度器（``run_scheduler``）、
          开发栈父进程（``run_dev_stack``）、MCP 服务（``run_mcp_server`` /
          ``python -m mcp_server``）、交互/维护命令（``sync_account_funds`` /
          ``gm_live_link`` / ``clear_intraday`` / ``purge_execution_logs``）
          以及采样子进程自身（``run_intraday_updater``，避免递归）都不启动。
          分时采样与写库只由 Web 服务进程负责，避免多进程重复外部请求。
          ⚠️ 用白名单而非黑名单的原因见下方注释（管道 EOF 永不关闭）；
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

        # ⚠️ 这里用**白名单**而不是黑名单，理由是"管道 EOF 永不关闭"这个坑：
        #
        # 采样子进程是**真正的子进程**（``subprocess.Popen``），会继承父进程的
        # stdout/stderr 管道。而任何用 ``subprocess.run(capture_output=True)``
        # 捕获输出的调用方，都要**读到 EOF** 才返回；父进程退出后，写端仍被
        # 采样器孙进程持有 → EOF 永不到达 → 调用方一直等到超时。
        #
        # 真实踩过：Web 的"同步账户资金"按钮在子进程里跑
        # ``manage.py sync_account_funds``，该命令不在黑名单里 → 子进程拉起了
        # 采样器 → 父进程的 ``capture_output`` 读不到 EOF → 20s 后报
        # "gm 终端无响应（20s 内未返回）"，**而 gm 其实完全正常**。
        #
        # 黑名单要求把每个新增的一次性命令都补进去，漏一个就重现；
        # 白名单则默认安全：新命令不会误拉采样器。
        #
        # 只有 **Web 服务进程**才采样分时数据——它才是这份数据的消费者。
        # 需覆盖各部署形态的入口标记（生产 Windows 走 ``python -m waitress``，
        # 不是 ``runserver``；漏掉它会让生产的分时采样直接停摆）：
        #   runserver（Django 开发服务器）/ waitress（Windows 生产）
        #   gunicorn / uvicorn（其它生产形态）
        #
        # ⚠️ 必须对**整条命令行**做子串匹配，不能用 ``in sys.argv``：
        # ``python -m waitress`` 的 ``sys.argv[0]`` 是
        # ``...\\waitress\\__main__.py``（模块名**不在** argv 元素里），
        # 精确匹配会漏判 → 生产的采样器直接不起（真实踩过）。
        sampling_host_markers = ('runserver', 'waitress', 'gunicorn', 'uvicorn')
        argv_text = ' '.join(sys.argv)
        if not any(marker in argv_text for marker in sampling_host_markers):
            return
        # runserver 自动重载的父进程不启动（仅 ``RUN_MAIN=true`` 的子进程启动）
        if 'runserver' in argv_text and os.environ.get('RUN_MAIN') != 'true':
            return

        from .updater import SubprocessIntradayUpdater

        SubprocessIntradayUpdater().start()
