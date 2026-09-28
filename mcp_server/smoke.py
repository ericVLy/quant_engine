"""MCP 端到端冒烟测试（`manage.py mcp_smoke_test`）。

用真实 MCP 协议（SSE 客户端 → MCP 服务）跑通一条完整链路：

    Case×2（signal + executor） → Suite → 拓扑 → Plan
      → 发布（Case → Suite → Plan） → 读侧回读校验
      →（可选）受控触发 → 删除保护校验 → 清理

覆盖工具：``create_case`` / ``create_suite`` / ``update_suite_topology`` /
``create_plan``（写）、``list_plans`` / ``get_plan`` / ``list_cases`` /
``get_case`` / ``get_suite_topology`` / ``list_suite_runs``（读）、
``trigger_plan_execution``（受控触发）、``delete_plan`` / ``delete_suite`` /
``delete_case``（删除）；并顺带验证两条安全契约：

- **删除保护**：Case 仍被 Suite 引用时 ``delete_case`` 必须冲突（REST 语义 409）；
- **设计边界**：发布不在 MCP 范围内（MCP-20 只改 draft），
  故发布经 ``apps.*`` 服务层完成，与 REST 视图同一入口。

同步 / 异步分离：异步方法**只做 MCP 协议调用**（不碰 Django ORM），
同步方法**只做 DB 写入**（账户资金配置 / 发布 / 运行实例清理）；
调用方在阶段之间切换，避免在事件循环里执行同步 ORM。

依赖方向：本模块属 ``mcp_server`` 叶子包，只单向依赖 ``apps.*`` 与 ``mcp`` SDK。
"""
from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any, Callable, Protocol

__all__ = [
    'SmokeError',
    'REQUIRED_TOOLS',
    'McpSession',
    'SseMcpSession',
    'McpSmokeRunner',
]

#: 冒烟测试依赖的 MCP 工具集合；缺任一即视为服务装配异常。
REQUIRED_TOOLS = frozenset({
    # 配置写（MCP-20）
    'create_case', 'update_case', 'delete_case',
    'create_suite', 'update_suite', 'update_suite_topology', 'delete_suite',
    'create_plan', 'update_plan', 'delete_plan',
    # 只读
    'list_plans', 'get_plan', 'list_cases', 'get_case',
    'get_suite_topology', 'list_suite_runs',
    # 受控触发
    'trigger_plan_execution',
})

#: 冒烟夹具统一名称前缀（便于识别与人工核对）。
NAME_PREFIX = 'MCP冒烟'

#: 内置事件类型：Case.trigger / Plan.event_type 必须来自事件注册中心。
SUITE_INIT = 'SUITE_INIT'
CASE_COMPLETED = 'CASE_COMPLETED'

#: 冒烟测试为绑定账户自动创建资金配置行时的默认总资金（仅当该账户无配置时生效）。
DEFAULT_TOTAL_CAPITAL = 100000


class SmokeError(RuntimeError):
    """冒烟测试失败：带步骤名的可定位错误。"""


class McpSession(Protocol):
    """冒烟测试所需的最小 MCP 会话能力（便于注入假会话做无网络测试）。"""

    async def list_tools(self) -> list[str]:
        """返回服务端已注册的工具名列表。"""

    async def call(self, name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
        """调用工具。

        Returns:
            tuple: ``(isError, 解析后的返回体)``；返回体优先按 JSON 解析，
            解析失败时回退为原始文本（错误消息场景）。
        """


def _decode(result: Any) -> Any:
    """把 ``tools/call`` 结果归一为 Python 对象（JSON 优先，回退原文）。"""
    content = getattr(result, 'content', None) or []
    if not content:
        return None
    text = getattr(content[0], 'text', None)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _is_error(result: Any) -> bool:
    """读取 ``CallToolResult`` 的错误标志。

    mcp 2.x 的字段名为 ``is_error``（snake_case）；早期版本/其他实现可能用
    ``isError``。两者都读，避免因取不到属性而把失败静默当成成功。

    Args:
        result: ``ClientSession.call_tool`` 的返回值。

    Returns:
        bool: 工具调用是否失败（服务端抛异常）。
    """
    for attr in ('is_error', 'isError'):
        value = getattr(result, attr, None)
        if value is not None:
            return bool(value)
    return False


class SseMcpSession:
    """真实 SSE 会话：把 ``mcp`` SDK 适配为 :class:`McpSession`。

    Args:
        url: MCP SSE 端点（如 ``http://127.0.0.1:8765/sse``）。
        headers: 额外请求头；服务端启用令牌时传
            ``{'Authorization': 'Bearer <token>'}``。

    Raises:
        Exception: 连接失败 / 初始化失败时原样抛出（由调用方给出可定位提示）。
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = headers or {}
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    async def __aenter__(self) -> 'SseMcpSession':
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(
            sse_client(self.url, headers=self.headers or None)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        return False

    async def list_tools(self) -> list[str]:
        result = await self._session.list_tools()
        return [tool.name for tool in result.tools]

    async def call(self, name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
        result = await self._session.call_tool(name, args or {})
        return _is_error(result), _decode(result)


class McpSmokeRunner:
    """MCP 冒烟步骤编排器：异步方法只走 MCP 协议，同步方法只写数据库。

    Args:
        account_id: 绑定到 Plan 的交易账户 ID；留空则不绑定账户、不占用资金。
        allocated_capital: Plan 占用资金（大于 0 时触发账户空闲资金校验）；留空=不占用。
        symbol_code: Plan 标的范围中的标的代码；须已存在于 ``watchlists.Symbol``。
        publish: MCP 建好 draft 夹具后是否发布（默认 True）。
        trigger: 是否执行受控触发（默认 False）。
        keep: 是否保留夹具不清理（默认 False，便于反复运行）。
        emit: 日志输出回调（默认静默；管理命令传入 ``self.stdout.write``）。

    Attributes:
        ids: 夹具主键（``signal_case_id`` / ``executor_case_id`` / ``suite_id`` / ``plan_id``）。
        steps: 已完成的步骤名（按执行顺序）。
        created_run_ids: 受控触发创建出的 SuiteRun 主键。
        purged_runs: 本次清理掉的 SuiteRun 行数。
    """

    def __init__(
        self,
        *,
        account_id: str = '',
        allocated_capital: float | None = None,
        symbol_code: str = '000426',
        publish: bool = True,
        trigger: bool = False,
        keep: bool = False,
        emit: Callable[[str], None] | None = None,
    ):
        self.account_id = account_id
        self.allocated_capital = allocated_capital
        self.symbol_code = symbol_code
        self.publish = publish
        self.trigger = trigger
        self.keep = keep
        self._emit = emit or (lambda message: None)
        self.ids: dict[str, int] = {}
        self.steps: list[str] = []
        self.created_run_ids: list[int] = []
        self.purged_runs = 0
        self._runs_purged = False

    # ------------------------------------------------------------------ 通用
    async def _call(
        self,
        session: McpSession,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        allow_error: bool = False,
    ) -> tuple[bool, Any]:
        """调用 MCP 工具；非预期错误直接抛 :class:`SmokeError`。"""
        is_error, body = await session.call(name, args)
        if is_error and not allow_error:
            detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            raise SmokeError(f'{name} 调用失败: {detail}')
        return is_error, body

    def _require(self, *names: str) -> None:
        missing = [name for name in names if not self.ids.get(name)]
        if missing:
            raise SmokeError(f'夹具未创建，缺少主键: {missing}')

    # ------------------------------------------------- 异步阶段：MCP 协议
    async def check_tools(self, session: McpSession) -> list[str]:
        """校验服务端工具清单包含冒烟所需的全部工具。

        Args:
            session: MCP 会话。

        Returns:
            list[str]: 服务端已注册的工具名（升序）。

        Raises:
            SmokeError: 缺少任一 :data:`REQUIRED_TOOLS` 中的工具。
        """
        names = sorted(await session.list_tools())
        missing = sorted(REQUIRED_TOOLS - set(names))
        if missing:
            raise SmokeError(f'MCP 服务缺少工具: {missing}')
        self._emit(f'[smoke] tools/list {len(names)} 个；冒烟依赖 {len(REQUIRED_TOOLS)} 个齐全')
        self.steps.append('check_tools')
        return names

    async def create_fixture(self, session: McpSession) -> dict[str, int]:
        """用 MCP 写工具创建 draft 夹具：Case×2 → Suite → 拓扑 → Plan。

        Args:
            session: MCP 会话。

        Returns:
            dict: 夹具主键（写入 :attr:`ids`）。

        Raises:
            SmokeError: 任一写工具调用失败（如写门禁未开启、参数校验不通过）。
        """
        # 标的范围由 Case 声明（symbol_scope）：两个 Case 都声明同一个标的，
        # Plan 的标的集合 = 编排树内各 Case 声明的并集。
        symbol_scope = {'type': 'symbols', 'symbol_codes': [self.symbol_code]}
        signal_params = {
            'trigger': {'event_type': SUITE_INIT},
            'indicator': 'rsi',
            'direction': 1,
            'period': 14,
            'threshold_oversold': 30,
            'threshold_overbought': 70,
            'symbol_scope': symbol_scope,
        }
        executor_params = {
            'trigger': {'event_type': CASE_COMPLETED},
            'order': {'direction': 'buy', 'price': 12.5, 'volume': 100},
            'result': {
                'direction': 1,
                'payload': {'note': 'MCP 冒烟测试下单'},
                'order': {'direction': 'buy', 'price': 12.5, 'volume': 100},
            },
            'symbol_scope': symbol_scope,
        }
        _, signal_case = await self._call(session, 'create_case', {
            'name': f'{NAME_PREFIX} · RSI信号',
            'node_type': 'signal',
            'params': signal_params,
        })
        _, executor_case = await self._call(session, 'create_case', {
            'name': f'{NAME_PREFIX} · 买入执行器',
            'node_type': 'executor',
            'params': executor_params,
        })
        case_ids = [signal_case['id'], executor_case['id']]
        self.ids['signal_case_id'] = case_ids[0]
        self.ids['executor_case_id'] = case_ids[1]

        _, suite = await self._call(session, 'create_suite', {
            'name': f'{NAME_PREFIX} · Suite',
            'aggregate_method': 'weighted_sum',
            'case_ids': case_ids,
        })
        self.ids['suite_id'] = suite['id']

        await self._call(session, 'update_suite_topology', {
            'suite_id': suite['id'],
            'case_ids': case_ids,
            'edges': [],
        })

        plan_args: dict[str, Any] = {
            'name': f'{NAME_PREFIX} · Plan {self.symbol_code}',
            'root_suite_id': suite['id'],
            'trigger_type': 'manual',
            'suite_start_mode': 'manual',
            'exec_mode': 'serial',
            'retry_policy': {'max_retries': 1, 'delay_seconds': 5},
        }
        if self.account_id:
            plan_args['account_id'] = self.account_id
        if self.allocated_capital:
            plan_args['allocated_capital'] = self.allocated_capital
        _, plan = await self._call(session, 'create_plan', plan_args)
        self.ids['plan_id'] = plan['id']

        self.steps.append('create_fixture')
        self._emit(
            f'[smoke] MCP 建夹具完成: case={case_ids} suite={suite["id"]} plan={plan["id"]}'
        )
        return dict(self.ids)

    async def verify_reads(self, session: McpSession) -> dict[str, Any]:
        """读侧回读校验：夹具经 MCP 读工具可见且字段与写入一致。

        Args:
            session: MCP 会话。

        Returns:
            dict: 关键读数（``published_plans`` / ``symbol_count`` /
            ``topology_case_ids`` / ``suite_runs``）。

        Raises:
            SmokeError: 任一回读断言不成立。
        """
        self._require('plan_id', 'suite_id', 'signal_case_id', 'executor_case_id')
        case_ids = [self.ids['signal_case_id'], self.ids['executor_case_id']]
        status = 'published' if self.publish else 'draft'
        checks: dict[str, Any] = {}

        if self.publish:
            _, plans = await self._call(session, 'list_plans', {'status': status})
            published = [item['id'] for item in plans.get('plans', [])]
            if self.ids['plan_id'] not in published:
                raise SmokeError(f'已发布 Plan 列表未包含夹具 {self.ids["plan_id"]}')
            checks['published_plans'] = published

        _, plan = await self._call(session, 'get_plan',
                                  {'plan_id': self.ids['plan_id'], 'include_symbols': True})
        if plan.get('symbol_count') != 1:
            raise SmokeError(f'get_plan 标的解析异常: symbol_count={plan.get("symbol_count")}')
        symbols = [item.get('code') for item in plan.get('symbols', [])]
        if symbols != [self.symbol_code]:
            raise SmokeError(f'get_plan 标的与 Case 声明不一致: {symbols}')
        checks['symbol_count'] = plan['symbol_count']
        checks['symbols'] = symbols

        _, cases = await self._call(session, 'list_cases', {'status': status})
        listed = [item['id'] for item in cases.get('cases', [])]
        if not set(case_ids) <= set(listed):
            raise SmokeError(f'list_cases 未包含夹具 {case_ids}')

        _, case = await self._call(session, 'get_case', {'case_id': case_ids[0]})
        if case.get('params', {}).get('indicator') != 'rsi':
            raise SmokeError(f'get_case params 异常: {case.get("params")}')

        _, topo = await self._call(session, 'get_suite_topology',
                                  {'suite_id': self.ids['suite_id']})
        topology = topo.get('topology', {}) if isinstance(topo, dict) else {}
        if topology.get('case_ids') != case_ids:
            raise SmokeError(f'拓扑与写入不一致: {topology.get("case_ids")} != {case_ids}')
        checks['topology_case_ids'] = topology['case_ids']

        _, runs = await self._call(session, 'list_suite_runs', {'plan_id': self.ids['plan_id']})
        checks['suite_runs'] = runs.get('count')
        self.steps.append('verify_reads')
        self._emit(f'[smoke] 读侧校验通过: {checks}')
        return checks

    async def trigger_execution(self, session: McpSession) -> list[int]:
        """受控触发：为夹具 Plan + 标的创建 ``pending`` SuiteRun。

        Args:
            session: MCP 会话。

        Returns:
            list[int]: 新建 SuiteRun 主键（同时写入 :attr:`created_run_ids`）。

        Raises:
            SmokeError: 触发失败，或新建 SuiteRun 状态不是 ``pending``。
        """
        self._require('plan_id')
        _, result = await self._call(session, 'trigger_plan_execution', {
            'plan_id': self.ids['plan_id'],
            'symbols': [self.symbol_code],
        })
        run_ids = list(result.get('created_run_ids', []))
        if not run_ids:
            raise SmokeError(f'trigger_plan_execution 未创建 SuiteRun: {result}')
        self.created_run_ids = run_ids

        _, runs = await self._call(session, 'list_suite_runs', {'plan_id': self.ids['plan_id']})
        statuses = {item['id']: item['status'] for item in runs.get('runs', [])}
        for run_id in run_ids:
            if statuses.get(run_id) != 'pending':
                raise SmokeError(f'SuiteRun {run_id} 应为 pending，实际 {statuses.get(run_id)}')
        self.steps.append('trigger_execution')
        self._emit(f'[smoke] 受控触发创建 SuiteRun {run_ids}（pending，未下单）')
        return run_ids

    async def check_delete_protection(self, session: McpSession) -> None:
        """校验删除保护：Case 仍被 Suite 引用时 ``delete_case`` 必须冲突。

        Args:
            session: MCP 会话。

        Raises:
            SmokeError: 删除未被拒绝（说明 409 删除保护失效）。
        """
        self._require('signal_case_id')
        is_error, body = await self._call(
            session, 'delete_case', {'case_id': self.ids['signal_case_id']},
            allow_error=True,
        )
        if not is_error:
            raise SmokeError('删除保护失效：被 Suite 引用的 Case 竟被删除')
        detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.steps.append('check_delete_protection')
        self._emit(f'[smoke] 删除保护生效（409 语义）: {detail[:120]}')

    async def delete_fixture(self, session: McpSession) -> dict[str, Any]:
        """按依赖逆序删除夹具：Plan → Suite → Case×2。

        Args:
            session: MCP 会话。

        Returns:
            dict: ``{'deleted': [...]}``。

        Raises:
            SmokeError: 存在未清理的 SuiteRun（Plan 删除会被保护拒绝），
                或任一删除调用失败、或清理后 Case 仍存在。
        """
        self._require('plan_id', 'suite_id', 'signal_case_id', 'executor_case_id')
        if self.created_run_ids and not self._runs_purged:
            raise SmokeError('存在受控触发创建的 SuiteRun，请先执行 purge_runs() 再删除 Plan')

        deleted: list[str] = []
        for call_name, arg_name, key in (
            ('delete_plan', 'plan_id', 'plan_id'),
            ('delete_suite', 'suite_id', 'suite_id'),
            ('delete_case', 'case_id', 'executor_case_id'),
            ('delete_case', 'case_id', 'signal_case_id'),
        ):
            await self._call(session, call_name, {arg_name: self.ids[key]})
            deleted.append(f'{call_name}({self.ids[key]})')

        _, cases = await self._call(
            session, 'list_cases', {'status': 'published' if self.publish else 'draft'})
        remaining = [item['id'] for item in cases.get('cases', [])]
        leftover = [case_id for case_id in
                    (self.ids['signal_case_id'], self.ids['executor_case_id'])
                    if case_id in remaining]
        if leftover:
            raise SmokeError(f'清理后 Case 仍存在: {leftover}')
        self.steps.append('delete_fixture')
        self._emit(f'[smoke] 夹具已清理: {deleted}')
        return {'deleted': deleted}

    # ------------------------------------------------- 同步阶段：数据库
    def prepare_account(self) -> dict[str, Any]:
        """确保绑定账户存在资金配置行（``create_plan`` 资金校验的前置条件）。

        Returns:
            dict: ``{'account_id': str, 'created': bool, 'total_capital': str}``；
            ``account_id`` 为空时返回 ``{'account_id': '', 'created': False}``。

        Note:
            账户资金配置属运维配置（admin 模型），不在 MCP 工具面内，故此处直接写库。
        """
        if not self.account_id:
            return {'account_id': '', 'created': False}
        from apps.execution.models import AccountFundConfig

        config, created = AccountFundConfig.objects.get_or_create(
            account_id=self.account_id, defaults={'total_capital': DEFAULT_TOTAL_CAPITAL},
        )
        self.steps.append('prepare_account')
        self._emit(
            f'[smoke] 账户资金配置{"新建" if created else "已存在"}: '
            f'total={config.total_capital} available={config.available_capital}'
        )
        return {
            'account_id': self.account_id,
            'created': created,
            'total_capital': str(config.total_capital),
        }

    def publish_fixture(self) -> dict[str, Any]:
        """发布夹具：Case → Suite → Plan（MCP 不做发布，走 apps 服务层）。

        发布顺序满足既有校验：Suite 发布要求全部 Case 已发布，
        Plan 发布要求根 Suite 已发布。

        Returns:
            dict: ``{'cases': [...], 'suite': {...}, 'plan': {...}}`` 状态摘要。

        Raises:
            apps.suites.services.SuiteError: 拓扑校验 / Case 未发布等失败。
            apps.plans.services.PlanError: 根 Suite 未发布。
            rest_framework.serializers.ValidationError: ``params`` 校验失败。
        """
        self._require('plan_id', 'suite_id', 'signal_case_id', 'executor_case_id')
        from apps.cases.models import Case
        from apps.cases.services import publish_case
        from apps.plans.models import Plan
        from apps.plans.services import publish_plan
        from apps.suites.models import Suite
        from apps.suites.services import publish_suite

        cases = []
        for case_id in (self.ids['signal_case_id'], self.ids['executor_case_id']):
            case = Case.objects.get(pk=case_id)
            if case.status != 'published':
                publish_case(case)
            cases.append(case)

        suite = Suite.objects.get(pk=self.ids['suite_id'])
        if suite.status != 'published':
            publish_suite(suite)

        plan = Plan.objects.select_related('root_suite').get(pk=self.ids['plan_id'])
        if plan.status != 'published':
            publish_plan(plan)

        self.steps.append('publish_fixture')
        self._emit(
            f'[smoke] 已发布: cases={[c.pk for c in cases]} '
            f'suite={suite.pk}(v{suite.version}) plan={plan.pk}(v{plan.version})'
        )
        return {
            'cases': [{'id': c.pk, 'status': c.status, 'version': c.version} for c in cases],
            'suite': {'id': suite.pk, 'status': suite.status, 'version': suite.version},
            'plan': {'id': plan.pk, 'status': plan.status, 'version': plan.version},
        }

    def purge_runs(self) -> int:
        """删除本次受控触发创建的 SuiteRun（Plan 删除保护的前置清理）。

        Returns:
            int: 删除行数（写入 :attr:`purged_runs`）。

        Note:
            仅删除 :attr:`created_run_ids` 中**本次调用自己创建**的行；
            历史运行轨迹由 ``SuiteRun`` / ``NodeRun`` / ``ExecutionLog`` 保留，
            冒烟测试自建自清，不触碰既有数据。重复调用幂等（第二次直接返回首次结果）。
        """
        if not self.created_run_ids or self._runs_purged:
            return self.purged_runs
        from apps.execution.models import SuiteRun

        deleted, _ = SuiteRun.objects.filter(pk__in=self.created_run_ids).delete()
        self.purged_runs = deleted
        self._runs_purged = True
        self.steps.append('purge_runs')
        self._emit(f'[smoke] 清理本次触发的 SuiteRun {self.created_run_ids}（{deleted} 行）')
        return deleted




