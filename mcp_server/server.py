"""MCPServer wiring for Quant Engine（SSE 为主传输的 Web 接入层）.

工具变量描述契约（MCP-19）：

- 每个工具参数的变量描述统一用 `Annotated[<类型>, Field(description=...)]` 声明，
  随 `tools/list` 下发的 `inputSchema.properties.<变量>.description` 一并给到 AI 客户端；
  mcp 2.x 不再解析函数 docstring 的参数段落，因此必须显式标注。
- 命令行参数（`--transport` / `--host` / `--port` / `--auth-token` / `--allow-trigger`）
  在 `_build_arg_parser()` 中逐个给出 `help` 文本。
- 配置项（`MCP_*` 环境变量）的变量说明见 `mcp_server/config.py` 模块文档与字段注释。
"""
from __future__ import annotations

import argparse
import logging
import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_server import tools_impl
from mcp_server.auth import BearerAuthMiddleware
from mcp_server.config import McpTransportConfig, load_transport_config

logger = logging.getLogger(__name__)


def create_server() -> MCPServer:
    """装配 MCP 服务：14 个工具 + 概览资源 + ``/health`` 路由。

    每个工具函数都用 ``Annotated[<类型>, Field(description=...)]`` 声明变量描述，
    注册到 ``tools/list`` 时由 SDK 转成
    ``inputSchema.properties.<变量>.description``；工具级说明仍通过
    ``@server.tool(description=...)`` 提供。

    Returns:
        MCPServer: 注册完毕的 MCP 服务实例（未启动传输）。
    """
    server = MCPServer(
        name='quant-engine',
        title='Quant Engine',
        description='量化投研与交易系统 MCP 接入：标的、K 线、策略元数据、告警与分时监控。',
        version='1.0.0',
        instructions=tools_impl.system_overview_text(),
    )

    @server.resource('quant://docs/overview', mime_type='text/plain')
    def overview_resource() -> str:
        return tools_impl.system_overview_text()

    @server.custom_route('/health', methods=['GET'], name='health')
    async def health_route(request: Any) -> Any:
        """健康检查（供部署探针使用；配置令牌后同样需要 Authorization 头）。"""
        from starlette.responses import JSONResponse

        return JSONResponse({
            'status': 'ok',
            'server': 'quant-engine',
            'transport': 'sse',
        })

    @server.tool(description='按代码或名称模糊搜索标的（watchlists.Symbol）。')
    def search_symbols(
        query: Annotated[
            str,
            Field(description='模糊关键字：标的代码或中文名称，支持部分匹配；留空不做关键字过滤。'),
        ] = '',
        market: Annotated[
            str,
            Field(description='市场过滤：A=A股、HK=港股、US=美股（大小写不敏感）；留空=全部市场。'),
        ] = '',
        limit: Annotated[
            int,
            Field(description='返回条数上限，取值范围 1~200（越界自动收敛），默认 50。'),
        ] = 50,
    ) -> dict:
        return tools_impl.search_symbols(query=query, market=market, limit=limit)

    @server.tool(description='解析标的代码对应的中文名称；优先读库内 Symbol。')
    def resolve_symbol_name(
        code: Annotated[
            str,
            Field(description='标的代码（如 000001）；带 sh/sz 前缀时保留指数/市场语义；不可为空。'),
        ],
        market: Annotated[
            str,
            Field(description='市场提示：A=A股、HK=港股、US=美股；仅库内未命中时用于回退解析，留空=不限。'),
        ] = '',
    ) -> dict:
        return tools_impl.resolve_symbol_name(code=code, market=market)

    @server.tool(description='查询标的 K 线（datasources 分表）；默认近 90 日，最多 500 根。')
    def query_kline(
        symbol_code: Annotated[
            str,
            Field(description='标的代码，需已存在于 watchlists.Symbol（如 000001）。'),
        ],
        start_date: Annotated[
            str,
            Field(description='起始日期 YYYY-MM-DD；留空=end_date 前 90 天。'),
        ] = '',
        end_date: Annotated[
            str,
            Field(description='结束日期 YYYY-MM-DD；留空=今天（服务器本地日期）。'),
        ] = '',
        limit: Annotated[
            int,
            Field(description='返回 K 线根数上限，取值 1~500（越界自动收敛）；超限时保留最近 N 根，默认 120。'),
        ] = 120,
    ) -> dict:
        return tools_impl.query_kline(
            symbol_code=symbol_code,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    @server.tool(description='列出 Plan（默认仅 published）。')
    def list_plans(
        status: Annotated[
            str,
            Field(description='状态过滤：draft=草稿、published=已发布、archived=已归档；留空=全部状态。'),
        ] = 'published',
        limit: Annotated[
            int,
            Field(description='返回条数上限，取值范围 1~200（越界自动收敛），默认 50。'),
        ] = 50,
    ) -> dict:
        return tools_impl.list_plans(status=status, limit=limit)

    @server.tool(description='Plan 详情；可选解析 symbol_scope 为标的列表。')
    def get_plan(
        plan_id: Annotated[
            int,
            Field(description='Plan 主键 ID；不存在时抛可定位的 ValueError。'),
        ],
        include_symbols: Annotated[
            bool,
            Field(description='是否将 symbol_scope 解析为标的列表（最多 500 条）并附 symbol_count，默认 True。'),
        ] = True,
    ) -> dict:
        return tools_impl.get_plan(plan_id=plan_id, include_symbols=include_symbols)

    @server.tool(description='列出 Case，可按 status / node_type 过滤。')
    def list_cases(
        status: Annotated[
            str,
            Field(description='状态过滤：draft=草稿、published=已发布、archived=已归档；留空=全部。'),
        ] = '',
        node_type: Annotated[
            str,
            Field(description='节点类型过滤：signal=信号、filter=过滤器、verdict=裁决、executor=执行器；留空=全部。'),
        ] = '',
        limit: Annotated[
            int,
            Field(description='返回条数上限，取值范围 1~200（越界自动收敛），默认 50。'),
        ] = 50,
    ) -> dict:
        return tools_impl.list_cases(status=status, node_type=node_type, limit=limit)

    @server.tool(description='Case 详情（含 params JSON）。')
    def get_case(
        case_id: Annotated[
            int,
            Field(description='Case 主键 ID；不存在时抛可定位的 ValueError。'),
        ],
    ) -> dict:
        return tools_impl.get_case(case_id=case_id)

    @server.tool(description='Suite 递归拓扑快照（build_topology_snapshot）。')
    def get_suite_topology(
        suite_id: Annotated[
            int,
            Field(description='Suite 主键 ID（作为递归拓扑快照的根节点）；不存在时抛可定位的 ValueError。'),
        ],
    ) -> dict:
        return tools_impl.get_suite_topology(suite_id=suite_id)

    @server.tool(description='列出已注册事件类型（系统内置 + EventTypeRegistry）。')
    def list_event_types(
        include_system: Annotated[
            bool,
            Field(description='是否包含系统内置事件（EventType）；False=仅 EventTypeRegistry 自定义类型，默认 True。'),
        ] = True,
    ) -> dict:
        return tools_impl.list_event_types(include_system=include_system)

    @server.tool(description='最近告警列表，可按 status / severity 过滤。')
    def list_alerts(
        status: Annotated[
            str,
            Field(description='状态过滤：pending=待处理、acknowledged=已确认、resolved=已解决；留空=全部。'),
        ] = '',
        severity: Annotated[
            str,
            Field(description='级别过滤：low=低、medium=中、high=高、critical=紧急；留空=全部。'),
        ] = '',
        limit: Annotated[
            int,
            Field(description='返回条数上限，取值范围 1~100（越界自动收敛），默认 30。'),
        ] = 30,
    ) -> dict:
        return tools_impl.list_alerts(status=status, severity=severity, limit=limit)

    @server.tool(description='告警统计（overview + by_type），与 REST /alerts/statistics/ 一致。')
    def alert_statistics() -> dict:
        return tools_impl.alert_statistics()

    @server.tool(description='当日分时序列（IntradayPoint）；不走外部网络。')
    def get_intraday_series(
        symbol_code: Annotated[
            str,
            Field(description='标的代码，需已存在于 watchlists.Symbol（如 000001）。'),
        ],
        limit: Annotated[
            int,
            Field(description='返回分时点上限，取值 1~500（越界自动收敛）；超限保留最近 N 点，默认 240。'),
        ] = 240,
    ) -> dict:
        return tools_impl.get_intraday_series(symbol_code=symbol_code, limit=limit)

    @server.tool(description='最近 SuiteRun 执行实例。')
    def list_suite_runs(
        plan_id: Annotated[
            int,
            Field(description='按 Plan 主键过滤；0 或留空=不过滤。'),
        ] = 0,
        symbol: Annotated[
            str,
            Field(description='按标的代码精确过滤；留空=不过滤。'),
        ] = '',
        limit: Annotated[
            int,
            Field(description='返回条数上限，取值范围 1~100（越界自动收敛），默认 20。'),
        ] = 20,
    ) -> dict:
        return tools_impl.list_suite_runs(plan_id=plan_id, symbol=symbol, limit=limit)

    @server.tool(
        description=(
            '为指定 Plan 与标的创建 pending SuiteRun（写操作）。'
            '默认禁用；需环境变量 MCP_ALLOW_TRIGGER=1，'
            '或启动 MCP 服务时加 --allow-trigger。'
        ),
    )
    def trigger_plan_execution(
        plan_id: Annotated[
            int,
            Field(description='Plan 主键 ID，必须为已发布（published）的 Plan。'),
        ],
        symbols: Annotated[
            list[str],
            Field(description='标的代码数组（如 000001、600000）；逐项去空格后各建一个 pending SuiteRun，空数组抛 ValueError。'),
        ],
    ) -> dict:
        return tools_impl.trigger_plan_execution(plan_id=plan_id, symbols=symbols)

    return server


def build_transport_security(config: McpTransportConfig) -> Any:
    """显式开启 DNS rebinding 保护（SDK 仅在绑定回环地址时自动开启）。

    绑定非回环地址时若不显式开启，Host / Origin 将不做校验，
    恶意网页可借浏览器直接访问本机（或内网）MCP 服务。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(config.allowed_hosts),
        allowed_origins=list(config.allowed_origins),
    )


def build_http_app(
    server: MCPServer | None = None,
    config: McpTransportConfig | None = None,
) -> Any:
    """构建 SSE（HTTP）ASGI 应用：MCP 端点 + 健康检查 + 鉴权 + 可选 CORS。

    - `transport_security` 显式开启 DNS rebinding 保护，Host / Origin 白名单由
      `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` 决定；
    - 配置 `MCP_AUTH_TOKEN` 时装配 `BearerAuthMiddleware`，`OPTIONS` 预检放行；
    - 配置 `MCP_CORS_ORIGINS` 时装配 `CORSMiddleware` 并置于最外层，
      使 401 响应也带上 CORS 头（便于浏览器客户端定位问题）。
    """
    server = server or create_server()
    config = (config or load_transport_config()).validate()

    app = server.sse_app(
        sse_path=config.sse_path,
        message_path=config.message_path,
        transport_security=build_transport_security(config),
        host=config.host,
    )
    if config.auth_enabled:
        app.add_middleware(BearerAuthMiddleware, token=config.auth_token)
    if config.cors_origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.cors_origins),
            allow_methods=['GET', 'POST', 'OPTIONS'],
            allow_headers=[
                'Authorization', 'Content-Type', 'Accept',
                'mcp-session-id', 'mcp-protocol-version',
            ],
            expose_headers=['mcp-session-id'],
        )
    return app


def run_http_server(
    config: McpTransportConfig | None = None,
    app: Any = None,
) -> None:
    """以 uvicorn 启动 SSE 服务（阻塞直到进程退出）。"""
    config = (config or load_transport_config()).validate()
    app = app or build_http_app(config=config)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - uvicorn 随 mcp 依赖安装
        raise RuntimeError('SSE 传输需要 uvicorn（随 `mcp` 依赖安装）') from exc

    logger.info(
        'MCP SSE 服务监听 %s · SSE=%s · 健康检查=%s · 鉴权=%s',
        config.bind_addr,
        config.sse_path,
        config.health_path,
        'Bearer 令牌' if config.auth_enabled else '关闭（仅回环地址）',
    )
    uvicorn.run(app, host=config.host, port=config.port, log_level='info')


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m mcp_server',
        description='Quant Engine MCP 服务（默认 SSE 传输；stdio 供本机 IDE 客户端）',
    )
    parser.add_argument(
        '--transport', choices=('sse', 'stdio'), default=None,
        help='传输方式：sse=HTTP/SSE 常驻服务（默认，供 Web / AI 助手按 URL 接入）；'
             'stdio=以子进程方式供本机 IDE 客户端接入，默认取 MCP_TRANSPORT',
    )
    parser.add_argument(
        '--host', default=None,
        help='监听地址，默认取 MCP_HOST（缺省 127.0.0.1 回环）；'
             '绑定非回环地址（如 0.0.0.0）时必须同时提供 --auth-token',
    )
    parser.add_argument(
        '--port', type=int, default=None,
        help='监听端口，取值范围 1~65535，默认取 MCP_PORT（缺省 8765）',
    )
    parser.add_argument(
        '--auth-token', dest='auth_token', default=None,
        help='Bearer 令牌，默认取 MCP_AUTH_TOKEN；绑定非回环地址时必填，'
             '客户端需在 Authorization 头携带 Bearer <token>',
    )
    parser.add_argument(
        '--allow-trigger', dest='allow_trigger', action='store_true', default=None,
        help='允许 trigger_plan_execution 写操作（等效 MCP_ALLOW_TRIGGER=1；'
             '布尔开关，不接受附加值；仅创建 pending SuiteRun，不直接下单）',
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    config = load_transport_config().with_overrides(
        transport=args.transport,
        host=args.host,
        port=args.port,
        auth_token=args.auth_token,
        allow_trigger=args.allow_trigger,
    )

    # CLI 显式开启时回写当前进程环境，复用工具层的写操作门禁；
    # 未传参数则保留环境变量，默认仍为只读。
    if args.allow_trigger:
        os.environ['MCP_ALLOW_TRIGGER'] = '1'

    from mcp_server.bootstrap import setup_django

    setup_django()

    if config.transport == 'stdio':
        # stdio 模式下 stdout 是协议通道，禁止打印任何日志
        create_server().run(transport='stdio')
        return

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    run_http_server(config=config)
