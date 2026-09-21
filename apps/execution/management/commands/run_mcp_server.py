"""启动 MCP 服务（模块11）：默认 SSE（HTTP）传输，供 Web / AI 助手接入。

与 `run_scheduler` 同一运维约定：入口放管理命令，运行期逻辑全部在 `mcp_server` 包内。
配置通过环境变量（`MCP_*`）或命令行参数提供，见 `mcp_server/config.py`。
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        '启动 Quant Engine MCP 服务（默认 SSE 传输；'
        '`--transport stdio` 供本机 IDE 客户端以子进程方式接入）'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--transport', choices=('sse', 'stdio'), default=None,
            help='传输方式：sse=HTTP/SSE 常驻服务（默认）；stdio=子进程供本机 IDE 接入，'
                 '默认取 MCP_TRANSPORT',
        )
        parser.add_argument(
            '--host', default=None,
            help='监听地址，默认取 MCP_HOST（缺省 127.0.0.1 回环）；'
                 '绑定非回环地址时必须同时提供 --auth-token',
        )
        parser.add_argument(
            '--port', type=int, default=None,
            help='监听端口，取值范围 1~65535，默认取 MCP_PORT（缺省 8765）',
        )
        parser.add_argument(
            '--auth-token', dest='auth_token', default=None,
            help='Bearer 令牌，默认取 MCP_AUTH_TOKEN；绑定非回环地址时必填',
        )
        parser.add_argument(
            '--allow-trigger', dest='allow_trigger', action='store_true', default=False,
            help='允许 trigger_plan_execution 写操作（等效 MCP_ALLOW_TRIGGER=1；'
                 '布尔开关，仅创建 pending SuiteRun，不直接下单）',
        )

    def handle(self, *args, **options):
        from mcp_server.server import main

        argv: list[str] = []
        if options.get('transport'):
            argv += ['--transport', options['transport']]
        if options.get('host'):
            argv += ['--host', options['host']]
        if options.get('port'):
            argv += ['--port', str(options['port'])]
        if options.get('auth_token'):
            argv += ['--auth-token', options['auth_token']]
        if options.get('allow_trigger'):
            argv += ['--allow-trigger']
        main(argv)