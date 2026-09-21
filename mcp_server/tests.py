"""MCP 服务（模块11）测试：工具门面 + 装配层 + SSE 传输与安全边界。

需求编号：MCP-01 ~ MCP-20（见 documents.md 模块11）。
只读工具直接查库；写操作仅"创建 pending SuiteRun"与"受控配置写"，不涉及任何真实下单。
"""
import ast
import asyncio
import inspect
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.cases.models import Case
from apps.execution.models import Alert, Order, SuiteRun
from apps.execution.registry import EventRegistry
from apps.execution.services import ExecutionError
from apps.monitoring.models import IntradayPoint
from apps.plans.models import Plan
from apps.suites.models import Suite
from apps.users.models import User
from apps.watchlists.models import Symbol

from mcp_server import tools_impl
from mcp_server.config import McpConfigError, load_transport_config
from mcp_server.formatting import to_jsonable
from mcp_server.server import build_http_app, build_transport_security, create_server

EXPECTED_TOOL_NAMES = {
    'search_symbols',
    'resolve_symbol_name',
    'query_kline',
    'list_plans',
    'get_plan',
    'list_cases',
    'get_case',
    'get_suite_topology',
    'list_event_types',
    'list_alerts',
    'alert_statistics',
    'get_intraday_series',
    'list_suite_runs',
    'trigger_plan_execution',
    'create_case',
    'update_case',
    'delete_case',
    'create_suite',
    'update_suite',
    'update_suite_topology',
    'delete_suite',
    'create_plan',
    'update_plan',
    'delete_plan',
}


class McpToolsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mcp_user', password='test')
        self.symbol = Symbol.objects.create(
            code='000001', name='平安银行', market='A', exchange='SZSE',
        )
        self.case = Case.objects.create(
            name='mcp-case',
            node_type='signal',
            params={'trigger': {'event_type': 'SUITE_INIT'}},
            created_by=self.user,
        )
        self.suite = Suite.objects.create(
            name='mcp-suite',
            created_by=self.user,
        )
        self.suite.cases.add(self.case)
        self.plan = Plan.objects.create(
            name='mcp-plan',
            root_suite=self.suite,
            trigger_type='manual',
            symbol_scope={'type': 'symbols', 'symbol_codes': ['000001']},
            created_by=self.user,
        )

    def test_search_symbols(self):
        result = tools_impl.search_symbols(query='平安', limit=10)
        self.assertGreaterEqual(result['count'], 1)
        self.assertEqual(result['symbols'][0]['code'], '000001')

    def test_resolve_symbol_name_from_db(self):
        result = tools_impl.resolve_symbol_name('000001')
        self.assertEqual(result['name'], '平安银行')
        self.assertEqual(result['symbol']['market'], 'A')

    def test_list_plans_and_get_plan(self):
        listed = tools_impl.list_plans(status='draft', limit=10)
        self.assertGreaterEqual(listed['count'], 1)
        detail = tools_impl.get_plan(self.plan.id, include_symbols=True)
        self.assertEqual(detail['name'], 'mcp-plan')
        self.assertEqual(detail['symbol_count'], 1)

    def test_get_case_and_suite_topology(self):
        case = tools_impl.get_case(self.case.id)
        self.assertEqual(case['node_type'], 'signal')
        topo = tools_impl.get_suite_topology(self.suite.id)
        self.assertEqual(topo['topology']['suite_id'], self.suite.id)
        self.assertIn(self.case.id, topo['topology']['case_ids'])

    def test_list_event_types(self):
        result = tools_impl.list_event_types(include_system=True)
        self.assertGreater(result['count'], 5)
        names = {item['name'] for item in result['event_types']}
        self.assertIn('SUITE_INIT', names)
        self.assertTrue(EventRegistry.validate('SUITE_INIT'))

    def _fake_bars(self, count):
        """构造与 datasources.query_kline_table 返回结构一致的伪 K 线。"""
        return [
            {
                'symbol': '000001',
                'date': date(2026, 1, 1) + timedelta(days=index),
                'open': Decimal('10.0000'),
                'high': Decimal('11.0000'),
                'low': Decimal('9.0000'),
                'close': Decimal('10.5000'),
                'volume': 1000 + index,
                'amount': Decimal('10500.00'),
                'extra': {'adj_factor': None, 'turnover_rate': None},
            }
            for index in range(count)
        ]

    def test_search_symbols_filters_market_and_clamps_limit(self):
        """MCP-02：market 过滤生效；limit 收敛到 1~200。"""
        self.assertEqual(tools_impl.search_symbols(market='HK')['count'], 0)
        clamped = tools_impl.search_symbols(query='000001', limit=0)
        self.assertEqual(clamped['count'], 1)
        self.assertEqual(clamped['symbols'][0]['exchange'], 'SZSE')

    def test_resolve_symbol_name_requires_code(self):
        """MCP-03：code 为空时明确报错，不静默返回空名称。"""
        with self.assertRaises(ValueError):
            tools_impl.resolve_symbol_name('   ')

    def test_resolve_symbol_name_falls_back_to_service_for_unknown_code(self):
        """MCP-03：库内无该标的时回退 watchlists 服务解析，并标记 symbol 为 None。"""
        with mock.patch(
            'apps.watchlists.services.resolve_symbol_name',
            return_value='回退名称',
        ) as patched:
            result = tools_impl.resolve_symbol_name('600000')
        self.assertEqual(result['name'], '回退名称')
        self.assertIsNone(result['symbol'])
        patched.assert_called_once_with('600000', None)

    def test_query_kline_uses_requested_window_and_json_safe_bars(self):
        """MCP-04：按显式日期窗口查分表，Decimal/date 输出为 JSON 安全字符串。"""
        rows = self._fake_bars(3)
        with mock.patch(
            'apps.datasources.services.query_kline_table',
            return_value=rows,
        ) as patched:
            result = tools_impl.query_kline(
                '000001', start_date='2026-01-01', end_date='2026-01-31', limit=10,
            )
        called_symbol, start, end = patched.call_args.args
        self.assertEqual(called_symbol.pk, self.symbol.pk)
        self.assertEqual((start, end), (date(2026, 1, 1), date(2026, 1, 31)))
        self.assertEqual(result['market'], 'A')
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['bars'][0]['date'], '2026-01-01')
        self.assertEqual(result['bars'][0]['close'], '10.5000')

    def test_query_kline_defaults_to_recent_window_and_keeps_tail(self):
        """MCP-04：缺省窗口为近 90 日；超出 limit 时保留最近 N 根。"""
        with mock.patch(
            'apps.datasources.services.query_kline_table',
            return_value=self._fake_bars(501),
        ) as patched:
            result = tools_impl.query_kline('000001', limit=9999)
        _, start, end = patched.call_args.args
        self.assertEqual(end, date.today())
        self.assertEqual(start, date.today() - timedelta(days=90))
        self.assertEqual(result['count'], 500)
        self.assertEqual(result['bars'][0]['date'], '2026-01-02')

    def test_query_kline_rejects_invalid_input(self):
        """MCP-04：空代码 / 未入库标的 / 反向日期窗口均拒绝。"""
        with self.assertRaises(ValueError):
            tools_impl.query_kline('')
        with self.assertRaises(ValueError):
            tools_impl.query_kline('999999')
        with self.assertRaises(ValueError):
            tools_impl.query_kline(
                '000001', start_date='2026-02-01', end_date='2026-01-01',
            )

    def test_trigger_plan_blocked_by_default(self):
        with self.assertRaises(PermissionError):
            tools_impl.trigger_plan_execution(self.plan.id, ['000001'])

    def test_list_cases_filters_by_node_type_and_status(self):
        """MCP-06：Case 列表按 node_type / status 过滤。"""
        Case.objects.create(
            name='mcp-executor', node_type='executor', params={}, created_by=self.user,
        )
        self.assertEqual(tools_impl.list_cases(node_type='executor')['count'], 1)
        self.assertEqual(tools_impl.list_cases(status='draft')['count'], 2)

    def test_missing_entities_raise_value_error(self):
        """MCP-06/07：资源不存在或参数为空时抛出可定位的 ValueError。"""
        with self.assertRaises(ValueError):
            tools_impl.get_plan(999999)
        with self.assertRaises(ValueError):
            tools_impl.get_case(999999)
        with self.assertRaises(ValueError):
            tools_impl.get_suite_topology(999999)
        with self.assertRaises(ValueError):
            tools_impl.get_intraday_series('')

    def test_list_alerts_and_statistics(self):
        """MCP-08：告警列表过滤与统计口径与 REST 契约一致。"""
        Alert.objects.create(
            alert_type='order_failed', severity='high', status='pending',
            title='下单失败', message='broker rejected',
        )
        Alert.objects.create(
            alert_type='risk_violation', severity='critical', status='resolved',
            title='风控拦截', message='single order limit',
        )
        self.assertEqual(tools_impl.list_alerts(status='pending')['count'], 1)
        self.assertEqual(
            tools_impl.list_alerts(severity='high')['alerts'][0]['title'], '下单失败',
        )
        stats = tools_impl.alert_statistics()
        self.assertEqual(stats['overview']['total'], 2)
        self.assertEqual(stats['overview']['pending'], 1)
        self.assertEqual(stats['overview']['resolved'], 1)
        self.assertEqual(stats['overview']['high_severity'], 1)
        self.assertEqual(stats['overview']['critical_severity'], 1)
        by_type = {row['alert_type']: row['count'] for row in stats['by_type']}
        self.assertEqual(by_type['order_failed'], 1)

    def test_list_suite_runs_filters_by_plan_and_symbol(self):
        """MCP-09：SuiteRun 列表按 plan_id / symbol 过滤。"""
        SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001',
            status='pending', event_queue=[],
        )
        SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='600000',
            status='completed', event_queue=[],
        )
        self.assertEqual(tools_impl.list_suite_runs(plan_id=self.plan.id)['count'], 2)
        filtered = tools_impl.list_suite_runs(plan_id=self.plan.id, symbol='600000')
        self.assertEqual(filtered['count'], 1)
        self.assertEqual(filtered['runs'][0]['status'], 'completed')

    def test_trigger_plan_creates_pending_runs_when_enabled(self):
        """MCP-10：显式开启后只创建 pending SuiteRun，绝不产生真实委托单。"""
        Plan.objects.filter(pk=self.plan.pk).update(status='published')
        with mock.patch.dict(os.environ, {'MCP_ALLOW_TRIGGER': '1'}):
            result = tools_impl.trigger_plan_execution(self.plan.id, ['000001', '600000'])
        self.assertEqual(result['count'], 2)
        runs = list(SuiteRun.objects.filter(plan=self.plan).order_by('symbol'))
        self.assertEqual([run.symbol for run in runs], ['000001', '600000'])
        self.assertTrue(all(run.status == 'pending' for run in runs))
        self.assertTrue(all(len(run.event_queue) == 1 for run in runs))
        self.assertEqual(result['created_run_ids'], [run.pk for run in runs])
        self.assertEqual(Order.objects.count(), 0)

    def test_trigger_plan_rejects_invalid_requests_when_enabled(self):
        """MCP-10：开启后仍拒绝空标的列表与未发布 Plan。"""
        with mock.patch.dict(os.environ, {'MCP_ALLOW_TRIGGER': 'true'}):
            with self.assertRaises(ValueError):
                tools_impl.trigger_plan_execution(self.plan.id, [])
            with self.assertRaises(ExecutionError):
                tools_impl.trigger_plan_execution(self.plan.id, ['000001'])
        self.assertEqual(SuiteRun.objects.count(), 0)

    def test_get_intraday_series_returns_points(self):
        """MCP-11：分时序列返回市场时区、时段状态与序列化点。"""
        for offset in (2, 1):
            IntradayPoint.objects.create(
                symbol=self.symbol,
                ts=timezone.now() - timedelta(minutes=offset),
                price=Decimal('10.5000'),
                change=Decimal('2.9400'),
                volume=1200,
                amount=Decimal('12600.00'),
                avg_price=Decimal('10.4800'),
                high=Decimal('10.6000'),
                low=Decimal('10.3000'),
                open_price=Decimal('10.2000'),
                pre_close=Decimal('10.2000'),
            )
        result = tools_impl.get_intraday_series('000001', limit=1)
        self.assertEqual(result['market'], 'A')
        self.assertEqual(result['timezone'], 'Asia/Shanghai')
        self.assertEqual(result['pre_close'], '10.2000')
        self.assertEqual(result['count'], 1)
        self.assertIn(
            result['session_status'],
            {'trading', 'lunch_break', 'closed', 'pre_market'},
        )
        self.assertEqual(
            set(result['points'][0]),
            {
                'ts', 'local_time', 'price', 'change', 'volume', 'amount',
                'avg_price', 'high', 'low', 'open_price', 'pre_close',
            },
        )

    def test_to_jsonable_supports_decimals_dates_and_models(self):
        """MCP-12：MCP 输出统一 JSON 安全（Decimal→str、日期→ISO、模型→pk）。"""
        payload = to_jsonable({
            'decimal': Decimal('10.5000'),
            'date': date(2026, 1, 1),
            'datetime': datetime(2026, 1, 1, 9, 30, tzinfo=dt_timezone.utc),
            'model': self.symbol,
            'nested': [Decimal('1.25'), {'items': (1, None, True)}],
            'unknown': object(),
            None: 'none-key',
        })
        self.assertEqual(payload['decimal'], '10.5000')
        self.assertEqual(payload['date'], '2026-01-01')
        self.assertEqual(payload['datetime'], '2026-01-01T09:30:00+00:00')
        self.assertEqual(payload['model'], self.symbol.pk)
        self.assertEqual(payload['nested'], ['1.25', {'items': [1, None, True]}])
        self.assertIsInstance(payload['unknown'], str)
        self.assertEqual(payload['None'], 'none-key')

    def test_get_intraday_empty(self):
        result = tools_impl.get_intraday_series('000001')
        self.assertEqual(result['symbol'], '000001')
        self.assertEqual(result['count'], 0)


class McpServerWiringTest(SimpleTestCase):
    """MCP 服务装配层契约（MCP-01 / MCP-13）：工具、资源与进程职责注册。"""

    def test_server_registers_expected_tools_and_resource(self):
        """MCP-01：14 个只读/受控工具 + 概览资源全部注册，说明文本含写开关提示。"""
        server = create_server()
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertEqual(names, EXPECTED_TOOL_NAMES)
        resources = {
            str(resource.uri) for resource in asyncio.run(server.list_resources())
        }
        self.assertEqual(resources, {'quant://docs/overview'})
        self.assertEqual(server.name, 'quant-engine')
        self.assertIn('MCP_ALLOW_TRIGGER=1', server.instructions)

    def test_bootstrap_disables_intraday_updater_by_default(self):
        """MCP-13：MCP 进程默认不拉起分时更新器，避免多进程重复采样与外部请求。"""
        from mcp_server import bootstrap

        env = {
            key: value
            for key, value in os.environ.items()
            if key != 'MONITORING_UPDATER_ENABLED'
        }
        with mock.patch.dict(os.environ, env, clear=True):
            bootstrap.setup_django()
            self.assertEqual(os.environ['MONITORING_UPDATER_ENABLED'], '0')
        with mock.patch.dict(os.environ, {'MONITORING_UPDATER_ENABLED': '1'}):
            bootstrap.setup_django()
            self.assertEqual(os.environ['MONITORING_UPDATER_ENABLED'], '1')

    def test_system_overview_text_documents_guardrails(self):
        """MCP-01：概览文本明确「只读默认 + 触发需开关」的边界。"""
        text = tools_impl.system_overview_text()
        self.assertIn('MCP_ALLOW_TRIGGER=1', text)
        self.assertIn('SuiteRun', text)


class McpToolSchemaTest(SimpleTestCase):
    """MCP-19：tools/list 为每个变量下发描述，且与门面实现签名严格一致。"""

    @staticmethod
    def _tools() -> dict:
        return {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    @staticmethod
    def _variables(tool) -> dict:
        return dict(tool.input_schema.get('properties') or {})

    def test_every_tool_variable_has_description(self):
        """MCP-19：每个入参都在 inputSchema 中带描述，缺失即视为客户端无法理解该变量。"""
        tools = self._tools()
        self.assertEqual(set(tools), EXPECTED_TOOL_NAMES)
        missing: dict[str, list[str]] = {}
        for name, tool in tools.items():
            for variable, spec in self._variables(tool).items():
                description = str(spec.get('description') or '').strip()
                if len(description) < 6:
                    missing.setdefault(name, []).append(variable)
        self.assertEqual(missing, {})

    def test_schema_variables_match_facade_signature(self):
        """MCP-19：入参名与必填项集合必须与门面签名逐一对应（读/触发→tools_impl，写→mutations）。"""
        for name, tool in self._tools().items():
            with self.subTest(tool=name):
                facade = self._facade(name)
                parameters = inspect.signature(facade).parameters
                self.assertEqual(set(self._variables(tool)), set(parameters))
                self.assertEqual(
                    set(tool.input_schema.get('required') or []),
                    {
                        variable
                        for variable, param in parameters.items()
                        if param.default is inspect.Parameter.empty
                    },
                )

    def test_tool_and_facade_docstrings_document_variables(self):
        """MCP-19：工具级说明非空，门面 docstring 逐个变量给出 Args/Returns。"""
        for name, tool in self._tools().items():
            with self.subTest(tool=name):
                facade = self._facade(name)
                self.assertTrue(str(tool.description or '').strip())
                docstring = inspect.getdoc(facade) or ''
                self.assertIn('Returns:', docstring)
                signature = inspect.signature(facade)
                if signature.parameters:
                    self.assertIn('Args:', docstring)
                for variable in signature.parameters:
                    self.assertIn(f'{variable}:', docstring)

    @staticmethod
    def _facade(name: str):
        """按工具名取门面函数：写操作在 ``mutations``，其余在 ``tools_impl``。"""
        from mcp_server import mutations

        if name in mutations.__all__:
            return getattr(mutations, name)
        return getattr(tools_impl, name)


class McpVariablesDocumentationTest(SimpleTestCase):
    """MCP-19：命令行与配置变量同样逐个带说明，防止新增变量漏写文档。"""

    def test_cli_options_have_help_text(self):
        from apps.execution.management.commands.run_mcp_server import Command

        from mcp_server.server import _build_arg_parser

        mcp_options = {'transport', 'host', 'port', 'auth_token', 'allow_trigger', 'allow_mutate'}
        parsers = {
            'python -m mcp_server': _build_arg_parser(),
            'manage.py run_mcp_server': Command().create_parser('manage.py', 'run_mcp_server'),
        }
        for entry, parser in parsers.items():
            # 管理命令解析器还含 Django 通用参数（--verbosity 等），这里只校验 MCP 自有变量
            actions = {
                action.dest: action
                for action in parser._actions
                if action.dest in mcp_options
            }
            with self.subTest(entry=entry):
                self.assertEqual(set(actions), mcp_options)
            for dest, action in actions.items():
                with self.subTest(entry=entry, option=dest):
                    self.assertTrue(str(action.help or '').strip())

    def test_config_env_vars_are_documented(self):
        """MCP-19：代码读取的每个 MCP_* 变量都必须出现在 config.py 模块文档表格中。"""
        from mcp_server import config as config_module

        source = Path(config_module.__file__).read_text(encoding='utf-8')
        docstring = ast.get_docstring(ast.parse(source)) or ''
        env_vars = set(re.findall(r"'(MCP_[A-Z_]+)'", source))
        self.assertIn('MCP_ALLOW_TRIGGER', env_vars)
        self.assertEqual(sorted(name for name in env_vars if name not in docstring), [])


class McpTransportConfigTest(SimpleTestCase):
    """MCP-14：传输配置（SSE 默认、端口与路径校验、非回环绑定必须鉴权）。"""

    def test_defaults_target_sse_on_loopback(self):
        config = load_transport_config(env={})
        self.assertEqual(config.transport, 'sse')
        self.assertEqual(config.host, '127.0.0.1')
        self.assertEqual(config.port, 8765)
        self.assertEqual(config.sse_path, '/sse')
        self.assertEqual(config.message_path, '/messages/')
        self.assertEqual(config.sse_url, 'http://127.0.0.1:8765/sse')
        self.assertTrue(config.is_loopback)
        self.assertFalse(config.auth_enabled)

    def test_env_overrides_and_list_parsing(self):
        config = load_transport_config(env={
            'MCP_TRANSPORT': 'SSE',
            'MCP_PORT': '9100',
            'MCP_SSE_PATH': 'rpc/sse',
            'MCP_AUTH_TOKEN': ' secret-token ',
            'MCP_ALLOWED_HOSTS': '127.0.0.1:*, mcp.local',
            'MCP_ALLOWED_ORIGINS': 'http://localhost:5173',
            'MCP_CORS_ORIGINS': 'http://localhost:5173,http://127.0.0.1:5173',
        })
        self.assertEqual(config.transport, 'sse')
        self.assertEqual(config.port, 9100)
        self.assertEqual(config.sse_path, '/rpc/sse')
        self.assertEqual(config.auth_token, 'secret-token')
        self.assertTrue(config.auth_enabled)
        self.assertEqual(config.allowed_hosts, ('127.0.0.1:*', 'mcp.local'))
        self.assertEqual(config.allowed_origins, ('http://localhost:5173',))
        self.assertEqual(len(config.cors_origins), 2)

    def test_invalid_values_raise_config_error(self):
        with self.assertRaises(McpConfigError):
            load_transport_config(env={'MCP_TRANSPORT': 'websocket'})
        with self.assertRaises(McpConfigError):
            load_transport_config(env={'MCP_PORT': 'abc'})
        with self.assertRaises(McpConfigError):
            load_transport_config(env={'MCP_PORT': '70000'})

    def test_non_loopback_bind_requires_token(self):
        """安全边界：对外暴露必须先有令牌，否则拒绝启动。"""
        with self.assertRaises(McpConfigError):
            load_transport_config(env={'MCP_HOST': '0.0.0.0'})
        config = load_transport_config(env={'MCP_HOST': '0.0.0.0', 'MCP_AUTH_TOKEN': 'tk'})
        self.assertFalse(config.is_loopback)
        with self.assertRaises(McpConfigError):
            load_transport_config(env={
                'MCP_HOST': '0.0.0.0', 'MCP_AUTH_TOKEN': 'tk', 'MCP_ALLOWED_HOSTS': '*',
            })

    def test_with_overrides_applies_cli_arguments(self):
        config = load_transport_config(env={}).with_overrides(transport='stdio', port=9999)
        self.assertEqual(config.transport, 'stdio')
        self.assertEqual(config.port, 9999)
        kept = load_transport_config(env={}).with_overrides(transport=None, port=None)
        self.assertEqual(kept.transport, 'sse')
        self.assertEqual(kept.port, 8765)

    def test_allow_trigger_env_and_cli_override(self):
        """MCP-10 配置面：写开关默认关闭，环境变量或 CLI 参数均可开启。"""
        self.assertFalse(load_transport_config(env={}).allow_trigger)
        self.assertTrue(
            load_transport_config(env={'MCP_ALLOW_TRIGGER': '1'}).allow_trigger
        )
        self.assertTrue(
            load_transport_config(env={'MCP_ALLOW_TRIGGER': 'yes'}).allow_trigger
        )
        cli_on = load_transport_config(env={}).with_overrides(allow_trigger=True)
        self.assertTrue(cli_on.allow_trigger)
        # CLI 未传参（None）时不得覆盖环境变量语义
        cli_absent = load_transport_config(
            env={'MCP_ALLOW_TRIGGER': '1'}
        ).with_overrides(allow_trigger=None)
        self.assertTrue(cli_absent.allow_trigger)

    def test_cli_parser_accepts_allow_trigger(self):
        """两个 CLI 入口（python -m mcp_server / manage.py run_mcp_server）均支持 --allow-trigger。"""
        from mcp_server.server import _build_arg_parser

        self.assertTrue(_build_arg_parser().parse_args(['--allow-trigger']).allow_trigger)
        self.assertIsNone(_build_arg_parser().parse_args([]).allow_trigger)
        self.assertTrue(
            _build_arg_parser().parse_args(
                ['--transport', 'sse', '--allow-trigger']
            ).allow_trigger
        )


class McpTriggerCliTest(SimpleTestCase):
    """MCP-18：两个启动入口的写开关转发、兼容性与安全边界。"""

    def test_management_command_forwards_flag(self):
        from django.core.management import call_command

        for flags in ([], ['--allow-trigger']):
            with self.subTest(flags=flags), mock.patch('mcp_server.server.main') as main:
                call_command('run_mcp_server', *flags)
                main.assert_called_once_with(flags)

    def test_main_applies_gate_for_both_transports(self):
        from mcp_server.server import main

        for transport in ('sse', 'stdio'):
            for env_value, flags, enabled in (
                ('', [], False), ('0', [], False), ('1', [], True),
                ('true', [], True), ('yes', [], True),
                ('', ['--allow-trigger'], True), ('0', ['--allow-trigger'], True),
            ):
                with self.subTest(transport=transport, env=env_value, flags=flags):
                    with (
                        mock.patch.dict(os.environ, {'MCP_ALLOW_TRIGGER': env_value}, clear=True),
                        mock.patch('mcp_server.bootstrap.setup_django'),
                        mock.patch('mcp_server.server.create_server') as create,
                        mock.patch('mcp_server.server.run_http_server') as run_http,
                        mock.patch('apps.execution.services.trigger_plan', return_value=[]) as trigger,
                    ):
                        def check_gate(*args, **kwargs):
                            if enabled:
                                self.assertEqual(
                                    tools_impl.trigger_plan_execution(1, ['000001'])['count'], 0,
                                )
                                trigger.assert_called_once_with(1, ['000001'])
                            else:
                                with self.assertRaises(PermissionError):
                                    tools_impl.trigger_plan_execution(1, ['000001'])
                                trigger.assert_not_called()

                        run_http.side_effect = check_gate
                        create.return_value.run.side_effect = check_gate
                        main(['--transport', transport, *flags])
                        if transport == 'sse':
                            self.assertEqual(run_http.call_args.kwargs['config'].allow_trigger, enabled)
                            create.assert_not_called()
                        else:
                            create.return_value.run.assert_called_once_with(transport='stdio')
                            run_http.assert_not_called()
                        self.assertEqual(
                            os.environ['MCP_ALLOW_TRIGGER'], '1' if flags else env_value,
                        )

    def test_allow_trigger_does_not_bypass_bind_auth(self):
        from mcp_server.server import main

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch('mcp_server.bootstrap.setup_django') as setup,
            mock.patch('mcp_server.server.run_http_server') as run_http,
        ):
            with self.assertRaises(McpConfigError):
                main(['--host', '0.0.0.0', '--allow-trigger'])
            self.assertNotIn('MCP_ALLOW_TRIGGER', os.environ)
            setup.assert_not_called()
            run_http.assert_not_called()


class McpHttpAppTest(SimpleTestCase):
    """MCP-15/16：SSE 应用装配 —— 端点、健康检查、Bearer 鉴权、DNS rebinding 保护。"""

    def _get(self, app, path, headers=None):
        """直接驱动 ASGI 应用（httpx 未安装，故不使用 Starlette TestClient）。"""
        scope = {
            'type': 'http',
            'asgi': {'version': '3.0'},
            'http_version': '1.1',
            'method': 'GET',
            'scheme': 'http',
            'path': path,
            'raw_path': path.encode('utf-8'),
            'query_string': b'',
            'root_path': '',
            'headers': [
                (name.lower().encode('utf-8'), str(value).encode('utf-8'))
                for name, value in (headers or {}).items()
            ],
            'client': ('127.0.0.1', 54321),
            'server': ('127.0.0.1', 8765),
        }
        sent = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            sent.append(message)

        asyncio.run(app(scope, receive, send))
        status = next(
            message['status'] for message in sent if message['type'] == 'http.response.start'
        )
        body = b''.join(
            message.get('body', b'') for message in sent
            if message['type'] == 'http.response.body'
        )
        return status, body

    def test_routes_include_sse_messages_and_health(self):
        app = build_http_app(config=load_transport_config(env={}))
        paths = {getattr(route, 'path', None) for route in app.routes}
        self.assertIn('/sse', paths)
        self.assertIn('/messages', paths)
        self.assertIn('/health', paths)

    def test_health_open_without_token_and_requires_token_when_configured(self):
        host = {'host': '127.0.0.1:8765'}
        open_app = build_http_app(config=load_transport_config(env={}))
        status, body = self._get(open_app, '/health', host)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'ok')

        guarded = build_http_app(config=load_transport_config(env={'MCP_AUTH_TOKEN': 'tk'}))
        status, body = self._get(guarded, '/health', host)
        self.assertEqual(status, 401)
        self.assertIn('Bearer', body.decode('utf-8'))
        status, _ = self._get(guarded, '/health', {**host, 'authorization': 'Bearer wrong'})
        self.assertEqual(status, 401)
        status, body = self._get(guarded, '/health', {**host, 'authorization': 'Bearer tk'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['transport'], 'sse')

    def test_transport_security_follows_config(self):
        """DNS rebinding 保护必须显式开启，且白名单来自配置。"""
        config = load_transport_config(env={
            'MCP_ALLOWED_HOSTS': '127.0.0.1:*,mcp.local',
            'MCP_ALLOWED_ORIGINS': 'http://localhost:5173',
        })
        security = build_transport_security(config)
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertEqual(security.allowed_hosts, ['127.0.0.1:*', 'mcp.local'])
        self.assertEqual(security.allowed_origins, ['http://localhost:5173'])

    def test_dns_rebinding_protection_rejects_unknown_host(self):
        """Host 不在白名单时拒绝建连，避免被恶意站点借浏览器访问本机服务。"""
        app = build_http_app(config=load_transport_config(env={}))
        with self.assertRaises(ValueError) as raised:
            self._get(app, '/sse', {'host': 'evil.example.com'})
        self.assertIn('validation failed', str(raised.exception).lower())


class McpServiceProcessGateTest(TestCase):
    """MCP-17：MCP 服务进程不启动分时更新器（分时更新只由 Django 服务进程负责）。"""

    def _ready(self, argv, enabled=True):
        from django.apps import apps as django_apps

        app_config = django_apps.get_app_config('monitoring')
        with mock.patch.object(settings, 'MONITORING_UPDATER_ENABLED', enabled), \
                mock.patch.object(sys, 'argv', argv), \
                mock.patch('apps.monitoring.updater.get_updater') as get_updater:
            app_config.ready()
        return get_updater

    def test_mcp_service_process_skips_updater(self):
        """`run_mcp_server` 管理命令进程不启动分时更新器。

        `python -m mcp_server` 路径由 `bootstrap.setup_django()` 置
        `MONITORING_UPDATER_ENABLED=0` 兜底（见 MCP-13 用例）。
        """
        for argv in (
            ['manage.py', 'run_mcp_server'],
            ['manage.py', 'run_mcp_server', '--port', '9100'],
            ['manage.py', 'run_mcp_server', '--transport', 'sse'],
        ):
            get_updater = self._ready(argv)
            get_updater.assert_not_called()

    def test_disabled_setting_and_reloader_parent_skip_updater(self):
        self._ready(['manage.py', 'runserver'], enabled=True).assert_not_called()
        self._ready(['manage.py', 'run_mcp_server'], enabled=False).assert_not_called()

    def test_runserver_child_process_starts_updater(self):
        with mock.patch.dict(os.environ, {'RUN_MAIN': 'true'}):
            get_updater = self._ready(['manage.py', 'runserver', '127.0.0.1:8000'])
        get_updater.assert_called_once()
        get_updater.return_value.start.assert_called_once()


class McpMutationsTest(TestCase):
    """MCP-20：配置写门面（默认禁用 + 开启后与 REST 同源校验 + 409 语义）。"""

    def setUp(self):
        self.user = User.objects.create_user(username='mcp_mut', password='test')
        self.case = Case.objects.create(
            name='mut-case', node_type='signal',
            params={'trigger': {'event_type': 'SUITE_INIT'}}, created_by=self.user,
        )
        self.suite = Suite.objects.create(name='mut-suite', created_by=self.user)
        self.plan = Plan.objects.create(
            name='mut-plan', root_suite=self.suite,
            trigger_type='manual', symbol_scope={'type': 'all'}, created_by=self.user,
        )

    def test_all_mutations_blocked_by_default(self):
        """MCP-20：默认（无 MCP_ALLOW_MUTATE）10 个写操作全部 PermissionError，不落库。"""
        from mcp_server import mutations

        with self.assertRaises(PermissionError):
            mutations.create_case('x', 'signal')
        with self.assertRaises(PermissionError):
            mutations.update_case(self.case.id, name='x')
        with self.assertRaises(PermissionError):
            mutations.delete_case(self.case.id)
        with self.assertRaises(PermissionError):
            mutations.create_suite('x')
        with self.assertRaises(PermissionError):
            mutations.update_suite(self.suite.id, name='x')
        with self.assertRaises(PermissionError):
            mutations.update_suite_topology(self.suite.id, [], [])
        with self.assertRaises(PermissionError):
            mutations.delete_suite(self.suite.id)
        with self.assertRaises(PermissionError):
            mutations.create_plan('x', self.suite.id)
        with self.assertRaises(PermissionError):
            mutations.update_plan(self.plan.id, name='x')
        with self.assertRaises(PermissionError):
            mutations.delete_plan(self.plan.id)
        self.assertEqual(Case.objects.filter(name='x').count(), 0)
        self.assertEqual(Suite.objects.filter(name='x').count(), 0)
        self.assertEqual(Plan.objects.filter(name='x').count(), 0)

    def test_switch_is_independent_from_trigger_switch(self):
        """MCP-20：开启 MCP_ALLOW_TRIGGER 不会连带开启配置写（反之亦然）。"""
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_TRIGGER': '1'}):
            with self.assertRaises(PermissionError):
                mutations.create_case('x', 'signal')
        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(PermissionError):
                tools_impl.trigger_plan_execution(self.plan.id, ['000001'])

    def test_case_create_update_delete_roundtrip(self):
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            created = mutations.create_case(
                'round-case', 'executor',
                params={'order': {'direction': 'buy', 'price': 10, 'volume': 100}},
            )
            self.assertEqual(created['status'], 'draft')
            self.assertEqual(created['version'], 1)
            self.assertEqual(created['params']['order']['volume'], 100)

            updated = mutations.update_case(created['id'], name='round-case-2')
            self.assertEqual(updated['name'], 'round-case-2')
            self.assertEqual(updated['node_type'], 'executor')

            deleted = mutations.delete_case(created['id'])
        self.assertEqual(deleted, {'deleted': 'case', 'id': created['id']})
        self.assertFalse(Case.objects.filter(pk=created['id']).exists())

    def test_case_rejects_whitelist_and_registry_violations(self):
        """MCP-20：params 白名单 / 未注册事件 / 非法 order 与 REST 同源拒绝。"""
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(ValueError):
                mutations.create_case('bad', 'signal', params={'evil': 1})
            with self.assertRaises(ValueError):
                mutations.create_case('bad', 'signal', params={'trigger': {'event_type': 'NOPE'}})
            with self.assertRaises(ValueError):
                mutations.create_case(
                    'bad', 'executor',
                    params={'order': {'direction': 'buy', 'price': 0, 'volume': 100}},
                )
            with self.assertRaises(ValueError):
                mutations.update_case(999999, name='x')

    def test_delete_case_conflict_when_referenced(self):
        from mcp_server import mutations

        self.suite.cases.add(self.case)
        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(mutations.MutationConflictError):
                mutations.delete_case(self.case.id)
        self.assertTrue(Case.objects.filter(pk=self.case.id).exists())

    def test_suite_create_update_topology_delete_roundtrip(self):
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            created = mutations.create_suite(
                'round-suite', aggregate_method='vote', case_ids=[self.case.id],
            )
            self.assertEqual(created['status'], 'draft')
            self.assertEqual(created['cases'], [self.case.id])

            updated = mutations.update_suite(created['id'], name='round-suite-2')
            self.assertEqual(updated['name'], 'round-suite-2')

            result = mutations.update_suite_topology(
                created['id'], case_ids=[self.case.id],
                edges=[{
                    'from_suite': created['id'], 'to_suite': self.suite.id,
                    'event_condition': {'event_type': 'CASE_COMPLETED'},
                    'weight': 1.0,
                }],
            )
            self.assertEqual(result, {'topology_updated': created['id']})

            deleted = mutations.delete_suite(created['id'])
        self.assertEqual(deleted, {'deleted': 'suite', 'id': created['id']})
        self.assertFalse(Suite.objects.filter(pk=created['id']).exists())

    def test_update_suite_topology_rejects_invalid_condition(self):
        """MCP-20：自环边 / 非法 event_condition 与 REST 400 同源拒绝。"""
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(ValueError):
                mutations.update_suite_topology(
                    self.suite.id, case_ids=[self.case.id],
                    edges=[{
                        'from_suite': self.suite.id, 'to_suite': self.suite.id,
                        'event_condition': {'event_type': 'CASE_COMPLETED'},
                    }],
                )
            with self.assertRaises(ValueError):
                mutations.update_suite_topology(
                    self.suite.id, case_ids=[self.case.id],
                    edges=[{
                        'from_suite': self.suite.id, 'to_suite': self.suite.id,
                        'event_condition': {'event_type': 'CASE_COMPLETED', 'evil': 1},
                    }],
                )

    def test_delete_suite_conflict_when_plan_references(self):
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(mutations.MutationConflictError):
                mutations.delete_suite(self.suite.id)
        self.assertTrue(Suite.objects.filter(pk=self.suite.id).exists())

    def test_plan_create_update_delete_roundtrip(self):
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            created = mutations.create_plan(
                'round-plan', root_suite_id=self.suite.id,
                trigger_type='manual',
                symbol_scope={'type': 'symbols', 'symbol_codes': ['000001']},
            )
            self.assertEqual(created['status'], 'draft')
            self.assertEqual(created['symbol_scope']['symbol_codes'], ['000001'])

            updated = mutations.update_plan(created['id'], name='round-plan-2')
            self.assertEqual(updated['name'], 'round-plan-2')

            deleted = mutations.delete_plan(created['id'])
        self.assertEqual(deleted, {'deleted': 'plan', 'id': created['id']})
        self.assertFalse(Plan.objects.filter(pk=created['id']).exists())

    def test_plan_rejects_symbol_scope_and_cron_violations(self):
        """MCP-20：symbol_scope 白名单 / time 触发缺 cron 与 REST 400 同源。"""
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(ValueError):
                mutations.create_plan(
                    'bad', root_suite_id=self.suite.id, trigger_type='manual',
                    symbol_scope={'type': 'symbols'},
                )
            with self.assertRaises(ValueError):
                mutations.create_plan('bad', root_suite_id=self.suite.id, trigger_type='time')
            with self.assertRaises(ValueError):
                mutations.update_plan(999999, name='x')

    def test_delete_plan_conflict_with_existing_runs(self):
        from mcp_server import mutations

        SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001',
            status='pending', event_queue=[],
        )
        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(mutations.MutationConflictError):
                mutations.delete_plan(self.plan.id)
        self.assertTrue(Plan.objects.filter(pk=self.plan.id).exists())

    def test_suite_create_update_topology_delete_roundtrip(self):
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            created = mutations.create_suite(
                'round-suite', aggregate_method='vote', case_ids=[self.case.id],
            )
            self.assertEqual(created['status'], 'draft')
            self.assertEqual(created['cases'], [self.case.id])

            updated = mutations.update_suite(created['id'], name='round-suite-2')
            self.assertEqual(updated['name'], 'round-suite-2')

            result = mutations.update_suite_topology(
                created['id'], case_ids=[self.case.id],
                edges=[{
                    'from_suite': created['id'], 'to_suite': self.suite.id,
                    'event_condition': {'event_type': 'CASE_COMPLETED'},
                    'weight': 1.0,
                }],
            )
            self.assertEqual(result, {'topology_updated': created['id']})

            deleted = mutations.delete_suite(created['id'])
        self.assertEqual(deleted, {'deleted': 'suite', 'id': created['id']})
        self.assertFalse(Suite.objects.filter(pk=created['id']).exists())

    def test_update_suite_topology_rejects_invalid_condition(self):
        """MCP-20：非法 event_condition / 自环边经 SuiteError 映射为冲突错误。"""
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(mutations.MutationConflictError):
                mutations.update_suite_topology(
                    self.suite.id, case_ids=[self.case.id],
                    edges=[{
                        'from_suite': self.suite.id, 'to_suite': self.suite.id,
                        'event_condition': {'event_type': 'CASE_COMPLETED'},
                    }],
                )
            with self.assertRaises(mutations.MutationConflictError):
                mutations.update_suite_topology(
                    self.suite.id, case_ids=[self.case.id],
                    edges=[{
                        'from_suite': self.suite.id, 'to_suite': self.suite.id,
                        'event_condition': {'event_type': 'CASE_COMPLETED', 'evil': 1},
                    }],
                )

    def test_delete_suite_conflict_when_plan_references(self):
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(mutations.MutationConflictError):
                mutations.delete_suite(self.suite.id)
        self.assertTrue(Suite.objects.filter(pk=self.suite.id).exists())

    def test_plan_create_update_delete_roundtrip(self):
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            created = mutations.create_plan(
                'round-plan', root_suite_id=self.suite.id,
                trigger_type='manual',
                symbol_scope={'type': 'symbols', 'symbol_codes': ['000001']},
            )
            self.assertEqual(created['status'], 'draft')
            self.assertEqual(created['symbol_scope']['symbol_codes'], ['000001'])

            updated = mutations.update_plan(created['id'], name='round-plan-2')
            self.assertEqual(updated['name'], 'round-plan-2')

            deleted = mutations.delete_plan(created['id'])
        self.assertEqual(deleted, {'deleted': 'plan', 'id': created['id']})
        self.assertFalse(Plan.objects.filter(pk=created['id']).exists())

    def test_plan_rejects_symbol_scope_and_cron_violations(self):
        """MCP-20：symbol_scope 白名单 / time 触发缺 cron 与 REST 400 同源。"""
        from mcp_server import mutations

        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(ValueError):
                mutations.create_plan(
                    'bad', root_suite_id=self.suite.id, trigger_type='manual',
                    symbol_scope={'type': 'symbols'},
                )
            with self.assertRaises(ValueError):
                mutations.create_plan('bad', root_suite_id=self.suite.id, trigger_type='time')
            with self.assertRaises(ValueError):
                mutations.update_plan(999999, name='x')

    def test_delete_plan_conflict_with_existing_runs(self):
        from mcp_server import mutations

        SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001',
            status='pending', event_queue=[],
        )
        with mock.patch.dict(os.environ, {'MCP_ALLOW_MUTATE': '1'}):
            with self.assertRaises(mutations.MutationConflictError):
                mutations.delete_plan(self.plan.id)
        self.assertTrue(Plan.objects.filter(pk=self.plan.id).exists())
