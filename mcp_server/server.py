"""MCPServer wiring for Quant Engine（SSE 为主传输的 Web 接入层）.

工具变量描述契约（MCP-19）：

- 每个工具参数的变量描述统一用 `Annotated[<类型>, Field(description=...)]` 声明，
  随 `tools/list` 下发的 `inputSchema.properties.<变量>.description` 一并给到 AI 客户端；
  mcp 2.x 不再解析函数 docstring 的参数段落，因此必须显式标注。
- 命令行参数（`--transport` / `--host` / `--port` / `--auth-token` / `--allow-trigger` /
  `--allow-mutate`）在 `_build_arg_parser()` 中逐个给出 `help` 文本。
- 配置项（`MCP_*` 环境变量）的变量说明见 `mcp_server/config.py` 模块文档与字段注释。

配置写开关（MCP-20）：`create_case/update_case/delete_case/create_suite/update_suite/
update_suite_topology/delete_suite/create_plan/update_plan/delete_plan` 共 10 个写工具，
默认禁用，需 `MCP_ALLOW_MUTATE=1` 或 `--allow-mutate`；与执行开关 `MCP_ALLOW_TRIGGER`
相互独立。门面实现见 `mcp_server/mutations.py`。
"""
from __future__ import annotations

import argparse
import logging
import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_server import mutations, tools_impl
from mcp_server.auth import BearerAuthMiddleware
from mcp_server.config import McpTransportConfig, load_transport_config

logger = logging.getLogger(__name__)


def create_server() -> MCPServer:
    """装配 MCP 服务：24 个工具（14 只读/受控触发 + 10 受控配置写）+ 概览资源 + ``/health``。

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

    # -- 以下为配置写操作（MCP-20，默认禁用）--------------------------------
    # 与 trigger_plan_execution 相互独立：本组由 MCP_ALLOW_MUTATE=1 / --allow-mutate
    # 门禁，只改策略配置（draft），不发布、不启动、不下单。

    @server.tool(
        description=(
            '创建 Case（draft，不发布；写操作，默认禁用）。'
            '需环境变量 MCP_ALLOW_MUTATE=1 或启动参数 --allow-mutate；'
            'params 须满足白名单校验（与 REST /api/cases/ 同源）。'
        ),
    )
    def create_case(
        name: Annotated[
            str,
            Field(description='Case 名称，长度 1~100。'),
        ],
        node_type: Annotated[
            str,
            Field(description='节点类型：signal（信号）/ filter（过滤）/ verdict（裁决）/ executor（执行器）。'),
        ],
        params: Annotated[
            dict | None,
            Field(description='参数对象，须满足 Case.params 白名单（trigger/period/order/result 等；trigger.event_type 必须已注册）；留空=空参数草稿。'),
        ] = None,
    ) -> dict:
        return mutations.create_case(name=name, node_type=node_type, params=params)

    @server.tool(
        description=(
            '编辑 Case 的 name/node_type/params（partial 更新；写操作，默认禁用）。'
            '需 MCP_ALLOW_MUTATE=1 或 --allow-mutate；发布与状态变更不在 MCP 范围。'
        ),
    )
    def update_case(
        case_id: Annotated[
            int,
            Field(description='目标 Case 主键 ID。'),
        ],
        name: Annotated[
            str | None,
            Field(description='新名称（长度 1~100）；未提供保持不变。'),
        ] = None,
        node_type: Annotated[
            str | None,
            Field(description='新节点类型：signal / filter / verdict / executor；未提供保持不变。'),
        ] = None,
        params: Annotated[
            dict | None,
            Field(description='新参数对象（整体替换，须满足白名单）；未提供保持不变。'),
        ] = None,
    ) -> dict:
        return mutations.update_case(case_id=case_id, name=name, node_type=node_type, params=params)

    @server.tool(
        description=(
            '删除 Case（写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
            '被 Suite 引用时抛冲突错误（REST 语义 409）。'
        ),
    )
    def delete_case(
        case_id: Annotated[
            int,
            Field(description='目标 Case 主键 ID。'),
        ],
    ) -> dict:
        return mutations.delete_case(case_id=case_id)

    @server.tool(
        description=(
            '创建 Suite（draft，不发布；写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
            '拓扑（Case 挂载 + 出边）随后经 update_suite_topology 整体写入。'
        ),
    )
    def create_suite(
        name: Annotated[
            str,
            Field(description='Suite 名称，长度 1~100。'),
        ],
        aggregate_method: Annotated[
            str,
            Field(description='节点内 Case 聚合方式：weighted_sum（默认）/ vote / and / or。'),
        ] = 'weighted_sum',
        parent_id: Annotated[
            int | None,
            Field(description='父 Suite 主键（构成子 Suite 树）；留空=根 Suite。'),
        ] = None,
        case_ids: Annotated[
            list[int] | None,
            Field(description='挂载的 Case 主键数组；留空=建空 Suite，拓扑后续经 update_suite_topology 写入。'),
        ] = None,
        allocated_capital: Annotated[
            float | None,
            Field(description='占用资金（大于 0 的数值）；留空=不占用。'),
        ] = None,
    ) -> dict:
        return mutations.create_suite(
            name=name,
            aggregate_method=aggregate_method,
            parent_id=parent_id,
            case_ids=case_ids,
            allocated_capital=allocated_capital,
        )

    @server.tool(
        description=(
            '编辑 Suite 基本字段（partial 更新；写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
            '拓扑整体替换请用 update_suite_topology。'
        ),
    )
    def update_suite(
        suite_id: Annotated[
            int,
            Field(description='目标 Suite 主键 ID。'),
        ],
        name: Annotated[
            str | None,
            Field(description='新名称（长度 1~100）；未提供保持不变。'),
        ] = None,
        aggregate_method: Annotated[
            str | None,
            Field(description='新聚合方式：weighted_sum / vote / and / or；未提供保持不变。'),
        ] = None,
        parent_id: Annotated[
            int | None,
            Field(description='新父 Suite 主键；未提供保持不变。'),
        ] = None,
        case_ids: Annotated[
            list[int] | None,
            Field(description='新 Case 主键数组（整体替换挂载关系）；未提供保持不变。'),
        ] = None,
        allocated_capital: Annotated[
            float | None,
            Field(description='新占用资金（大于 0）；未提供保持不变。'),
        ] = None,
    ) -> dict:
        return mutations.update_suite(
            suite_id=suite_id,
            name=name,
            aggregate_method=aggregate_method,
            parent_id=parent_id,
            case_ids=case_ids,
            allocated_capital=allocated_capital,
        )

    @server.tool(
        description=(
            '整体替换 Suite 的 Case 挂载与出边（事务内，含 DAG 与 event_condition 校验；'
            '写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
        ),
    )
    def update_suite_topology(
        suite_id: Annotated[
            int,
            Field(description='目标 Suite 主键 ID；只允许替换该 Suite 的出边，目标不能指向自身。'),
        ],
        case_ids: Annotated[
            list[int] | None,
            Field(description='全量 Case 主键数组（整体替换；空数组=清空挂载）。'),
        ],
        edges: Annotated[
            list[dict] | None,
            Field(description='全量出边数组，每项 {from_suite, to_suite, condition, event_condition, weight}；event_condition 须满足白名单（event_type 必填 + 可选 op/field/threshold）；空数组=清空出边。'),
        ],
    ) -> dict:
        return mutations.update_suite_topology(suite_id=suite_id, case_ids=case_ids, edges=edges)

    @server.tool(
        description=(
            '删除 Suite（写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
            '被 Plan 引用时抛冲突错误（REST 语义 409）。'
        ),
    )
    def delete_suite(
        suite_id: Annotated[
            int,
            Field(description='目标 Suite 主键 ID。'),
        ],
    ) -> dict:
        return mutations.delete_suite(suite_id=suite_id)

    @server.tool(
        description=(
            '创建 Plan（draft，不发布；写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
            '复用 REST /api/plans/ 全部校验（cron、事件注册、symbol_scope 白名单、账户资金）。'
        ),
    )
    def create_plan(
        name: Annotated[
            str,
            Field(description='Plan 名称，长度 1~100。'),
        ],
        root_suite_id: Annotated[
            int,
            Field(description='根 Suite 主键；创建阶段不要求已发布（发布时才校验）。'),
        ],
        trigger_type: Annotated[
            str,
            Field(description='触发方式：time（时间驱动，需 cron_expr）/ event（事件驱动，需已注册 event_type）/ manual（手动触发，默认）。'),
        ] = 'manual',
        cron_expr: Annotated[
            str | None,
            Field(description='5 字段 cron 表达式（如 0 9 30 * *）；time 触发时必填；未提供保持缺省。'),
        ] = None,
        event_type: Annotated[
            str | None,
            Field(description='触发事件类型；event 触发时必填且必须已注册；未提供保持缺省。'),
        ] = None,
        symbol_scope: Annotated[
            dict | None,
            Field(description='标的范围，白名单 {type, group_ids, symbol_codes}：all 只含 type；groups 需 group_ids；symbols 需 symbol_codes；缺省 {"type":"all"}。'),
        ] = None,
        exec_mode: Annotated[
            str,
            Field(description='执行模式：serial（串行，默认）/ parallel（并行）/ fail_stop（失败停止）。'),
        ] = 'serial',
        retry_policy: Annotated[
            dict | None,
            Field(description='重试策略 {max_retries: >=0 整数, delay_seconds: >=0 数值}；留空=不重试。'),
        ] = None,
        account_id: Annotated[
            str,
            Field(description='交易账户 ID（64 字符内）；留空=不绑定账户。'),
        ] = '',
        allocated_capital: Annotated[
            float | None,
            Field(description='占用资金（大于 0）；与 account_id 一起触发账户空闲资金校验；留空=不占用。'),
        ] = None,
        suite_start_mode: Annotated[
            str,
            Field(description='Suite 启动模式：manual（默认）/ auto。'),
        ] = 'manual',
    ) -> dict:
        return mutations.create_plan(
            name=name,
            root_suite_id=root_suite_id,
            trigger_type=trigger_type,
            cron_expr=cron_expr,
            event_type=event_type,
            symbol_scope=symbol_scope,
            exec_mode=exec_mode,
            retry_policy=retry_policy,
            account_id=account_id,
            allocated_capital=allocated_capital,
            suite_start_mode=suite_start_mode,
        )

    @server.tool(
        description=(
            '编辑 Plan 配置字段（partial 更新；写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
            '发布 / 回滚 / 启停不在 MCP 范围，走 REST 动作接口。'
        ),
    )
    def update_plan(
        plan_id: Annotated[
            int,
            Field(description='目标 Plan 主键 ID。'),
        ],
        name: Annotated[
            str | None,
            Field(description='新名称（长度 1~100）；未提供保持不变。'),
        ] = None,
        root_suite_id: Annotated[
            int | None,
            Field(description='新根 Suite 主键；未提供保持不变。'),
        ] = None,
        trigger_type: Annotated[
            str | None,
            Field(description='新触发方式：time / event / manual；未提供保持不变。'),
        ] = None,
        cron_expr: Annotated[
            str | None,
            Field(description='新 5 字段 cron 表达式；未提供保持不变。'),
        ] = None,
        event_type: Annotated[
            str | None,
            Field(description='新触发事件类型（须已注册）；未提供保持不变。'),
        ] = None,
        symbol_scope: Annotated[
            dict | None,
            Field(description='新标的范围（白名单同 create_plan）；未提供保持不变。'),
        ] = None,
        exec_mode: Annotated[
            str | None,
            Field(description='新执行模式：serial / parallel / fail_stop；未提供保持不变。'),
        ] = None,
        retry_policy: Annotated[
            dict | None,
            Field(description='新重试策略 {max_retries, delay_seconds}；未提供保持不变。'),
        ] = None,
        account_id: Annotated[
            str | None,
            Field(description='新交易账户 ID；未提供保持不变。'),
        ] = None,
        allocated_capital: Annotated[
            float | None,
            Field(description='新占用资金（大于 0）；未提供保持不变。'),
        ] = None,
        suite_start_mode: Annotated[
            str | None,
            Field(description='新 Suite 启动模式：manual / auto；未提供保持不变。'),
        ] = None,
    ) -> dict:
        return mutations.update_plan(
            plan_id=plan_id,
            name=name,
            root_suite_id=root_suite_id,
            trigger_type=trigger_type,
            cron_expr=cron_expr,
            event_type=event_type,
            symbol_scope=symbol_scope,
            exec_mode=exec_mode,
            retry_policy=retry_policy,
            account_id=account_id,
            allocated_capital=allocated_capital,
            suite_start_mode=suite_start_mode,
        )

    @server.tool(
        description=(
            '删除 Plan（写操作，默认禁用；需 MCP_ALLOW_MUTATE=1 或 --allow-mutate）。'
            '已有执行记录（SuiteRun）时抛冲突错误（REST 语义 409）。'
        ),
    )
    def delete_plan(
        plan_id: Annotated[
            int,
            Field(description='目标 Plan 主键 ID。'),
        ],
    ) -> dict:
        return mutations.delete_plan(plan_id=plan_id)

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
    parser.add_argument(
        '--allow-mutate', dest='allow_mutate', action='store_true', default=None,
        help='允许配置写操作：创建/编辑/删除 Case、Suite、Plan（等效 MCP_ALLOW_MUTATE=1；'
             '布尔开关，不接受附加值；与 --allow-trigger 相互独立；'
             '仅改 draft 配置，不发布、不启动、不下单）',
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
    if args.allow_mutate:
        os.environ['MCP_ALLOW_MUTATE'] = '1'

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
