"""MCPServer wiring for Quant Engine."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from mcp_server import tools_impl


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
            '默认禁用；需环境变量 MCP_ALLOW_TRIGGER=1。'
        ),
    )
    def trigger_plan_execution(plan_id: int, symbols: list[str]) -> dict:
        return tools_impl.trigger_plan_execution(plan_id=plan_id, symbols=symbols)

    return server


def main() -> None:
    from mcp_server.bootstrap import setup_django

    setup_django()
    create_server().run(transport='stdio')
