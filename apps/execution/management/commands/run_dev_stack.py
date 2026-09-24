"""一键启动本地开发栈：Django 服务 + MCP 服务（+ 可选 Scheduler）。

用法：
    python manage.py run_dev_stack                       # 前台运行，Ctrl+C 一次性退出
    python manage.py run_dev_stack --web-port 8001 --mcp-port 8766
    python manage.py run_dev_stack --with-scheduler      # 附带策略调度器

说明：
  - Django 子进程负责分时更新器（updater 随服务进程启动）；
  - MCP 子进程默认只读（写开关经 --allow-trigger / --allow-mutate 透传）；
  - 任一子进程退出（崩溃或被外部终止）时，终止其余子进程并退出（fail-fast）；
  - 收到 SIGINT / SIGTERM 时向所有子进程转发终止信号，避免遗留孤儿进程。
"""
import os
import signal
import subprocess
import sys
import time

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = '一键启动开发栈：Django 服务 + MCP 服务（可选 Scheduler），单一命令统一启停'

    def add_arguments(self, parser):
        parser.add_argument('--web-host', default='127.0.0.1',
                            help='Django 服务监听地址（默认 127.0.0.1）')
        parser.add_argument('--web-port', type=int, default=8000,
                            help='Django 服务监听端口（默认 8000）')
        parser.add_argument('--mcp-host', default='127.0.0.1',
                            help='MCP 服务监听地址（默认 127.0.0.1；非回环必须配令牌）')
        parser.add_argument('--mcp-port', type=int, default=8765,
                            help='MCP 服务监听端口（默认 8765）')
        parser.add_argument('--mcp-auth-token', dest='mcp_auth_token', default=None,
                            help='MCP Bearer 令牌（默认取 MCP_AUTH_TOKEN；'
                                 'MCP 绑定非回环地址时必须提供）')
        parser.add_argument('--allow-trigger', dest='allow_trigger',
                            action='store_true', default=False,
                            help='透传 MCP 受控触发开关（等效 MCP_ALLOW_TRIGGER=1）')
        parser.add_argument('--allow-mutate', dest='allow_mutate',
                            action='store_true', default=False,
                            help='透传 MCP 配置写开关（等效 MCP_ALLOW_MUTATE=1）')
        parser.add_argument('--with-scheduler', dest='with_scheduler',
                            action='store_true', default=False,
                            help='同时启动策略调度器（run_scheduler --interval 60）')

    def handle(self, *args, **options):
        python = sys.executable
        manage_py = os.path.join(os.getcwd(), 'manage.py')
        if not os.path.exists(manage_py):
            raise CommandError('未找到 manage.py；请在项目根目录运行本命令')

        procs = []

        def launch(cmd, env=None):
            self.stdout.write(f'[dev-stack] 启动: {" ".join(cmd)}')
            return subprocess.Popen(cmd, env=env)

        # 1) Django 开发服务。--noreload 下无 RUN_MAIN 重载子进程，显式置
        #    RUN_MAIN=true 使分时更新器在该单进程内正常启动（MonitoringConfig 门禁）。
        procs.append(launch(
            [python, manage_py, 'runserver',
             f"{options['web_host']}:{options['web_port']}", '--noreload'],
            env={**os.environ, 'RUN_MAIN': 'true'},
        ))
        # 2) MCP 服务（SSE）
        mcp_cmd = [python, manage_py, 'run_mcp_server',
                   '--host', options['mcp_host'],
                   '--port', str(options['mcp_port'])]
        if options.get('mcp_auth_token'):
            mcp_cmd += ['--auth-token', options['mcp_auth_token']]
        if options.get('allow_trigger'):
            mcp_cmd += ['--allow-trigger']
        if options.get('allow_mutate'):
            mcp_cmd += ['--allow-mutate']
        procs.append(launch(mcp_cmd))
        # 3) 可选 Scheduler
        if options.get('with_scheduler'):
            procs.append(launch(
                [python, manage_py, 'run_scheduler', '--interval', '60']))

        def shutdown(signum, frame):
            self.stdout.write(f'[dev-stack] 收到信号 {signum}，正在停止全部子进程…')
            for p in procs:
                if p.poll() is None:
                    p.terminate()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        try:
            # 轮询等待；任一子进程退出即 fail-fast：终止其余子进程
            # （不用 signal.pause：信号只唤醒一次，子进程随后静默退出会让父进程永久阻塞）
            while procs:
                exited = []
                for p in procs:
                    code = p.poll()
                    if code is not None:
                        exited.append((p.args, code))
                if exited:
                    for args_, code in exited:
                        self.stdout.write(f'[dev-stack] 子进程退出: {args_} (code={code})')
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stdout.write('[dev-stack] 停止剩余子进程…')
            for p in procs:
                if p.poll() is None:
                    p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
            codes = [p.poll() for p in procs]
            if any(c not in (0, None, -15, -2) for c in codes):
                raise CommandError(f'部分子进程异常退出: {list(zip([p.args for p in procs], codes))}')
            self.stdout.write('[dev-stack] 已全部停止。')
