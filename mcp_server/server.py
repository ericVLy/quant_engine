"""MCPServer wiring for Quant Engine（SSE 为主传输的 Web 接入层）."""
from __future__ import annotations

import argparse
import logging
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_server import tools_impl
from mcp_server.auth import BearerAuthMiddleware
from mcp_server.config import McpTransportConfig, load_transport_config

logger = logging.getLogger(__name__)


def create_server() -> MCPServer:
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
    def search_symbols(query: str = '', market: str = '', limit: int = 50) -> dict:
        return tools_impl.search_symbols(query=query, market=market, limit=limit)

    @server.tool(description='解析标的代码对应的中文名称；优先读库内 Symbol。')
    def resolve_symbol_name(code: str, market: str = '') -> dict:
        return tools_impl.resolve_symbol_name(code=code, market=market)

    @server.tool(description='查询标的 K 线（datasources 分表）；默认近 90 日，最多 500 根。')
    def query_kline(
        symbol_code: str,
        start_date: str = '',
        end_date: str = '',
        limit: int = 120,
    ) -> dict:
        return tools_impl.query_kline(
            symbol_code=symbol_code,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    @server.tool(description='列出 Plan（默认仅 published）。')
    def list_plans(status: str = 'published', limit: int = 50) -> dict:
        return tools_impl.list_plans(status=status, limit=limit)

    @server.tool(description='Plan 详情；可选解析 symbol_scope 为标的列表。')
    def get_plan(plan_id: int, include_symbols: bool = True) -> dict:
        return tools_impl.get_plan(plan_id=plan_id, include_symbols=include_symbols)

    @server.tool(description='列出 Case，可按 status / node_type 过滤。')
    def list_cases(status: str = '', node_type: str = '', limit: int = 50) -> dict:
        return tools_impl.list_cases(status=status, node_type=node_type, limit=limit)

    @server.tool(description='Case 详情（含 params JSON）。')
    def get_case(case_id: int) -> dict:
        return tools_impl.get_case(case_id=case_id)

    @server.tool(description='Suite 递归拓扑快照（build_topology_snapshot）。')
    def get_suite_topology(suite_id: int) -> dict:
        return tools_impl.get_suite_topology(suite_id=suite_id)

    @server.tool(description='列出已注册事件类型（系统内置 + EventTypeRegistry）。')
    def list_event_types(include_system: bool = True) -> dict:
        return tools_impl.list_event_types(include_system=include_system)

    @server.tool(description='最近告警列表，可按 status / severity 过滤。')
    def list_alerts(status: str = '', severity: str = '', limit: int = 30) -> dict:
        return tools_impl.list_alerts(status=status, severity=severity, limit=limit)

    @server.tool(description='告警统计（overview + by_type），与 REST /alerts/statistics/ 一致。')
    def alert_statistics() -> dict:
        return tools_impl.alert_statistics()

    @server.tool(description='当日分时序列（IntradayPoint）；不走外部网络。')
    def get_intraday_series(symbol_code: str, limit: int = 240) -> dict:
        return tools_impl.get_intraday_series(symbol_code=symbol_code, limit=limit)

    @server.tool(description='最近 SuiteRun 执行实例。')
    def list_suite_runs(plan_id: int = 0, symbol: str = '', limit: int = 20) -> dict:
        return tools_impl.list_suite_runs(plan_id=plan_id, symbol=symbol, limit=limit)

    @server.tool(
        description=(
            '为指定 Plan 与标的创建 pending SuiteRun（写操作）。'
            '默认禁用；需环境变量 MCP_ALLOW_TRIGGER=1，'
            '或启动 MCP 服务时加 --allow-trigger。'
        ),
    )
    def trigger_plan_execution(plan_id: int, symbols: list[str]) -> dict:
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
    parser.add_argument('--transport', choices=('sse', 'stdio'), default=None)
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument(
        '--auth-token', dest='auth_token', default=None,
        help='Bearer 令牌，默认取 MCP_AUTH_TOKEN；绑定非回环地址时必填',
    )
    parser.add_argument(
        '--allow-trigger', dest='allow_trigger', action='store_true', default=None,
        help='允许 trigger_plan_execution 写操作（等效 MCP_ALLOW_TRIGGER=1）',
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
