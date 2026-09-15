"""MCP 服务（模块11）测试：工具门面 + 装配层。

需求编号：MCP-01 ~ MCP-09（见 documents.md 模块11）。
只读工具直接查库；写操作仅"创建 pending SuiteRun"，不涉及任何真实下单。
"""
import asyncio
import os
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

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
from mcp_server.formatting import to_jsonable
from mcp_server.server import create_server

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
