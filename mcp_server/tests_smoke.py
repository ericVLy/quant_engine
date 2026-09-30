"""``mcp_server.smoke`` 冒烟执行器测试。

覆盖：``_is_error`` / ``_decode`` 归一、工具清单校验、建夹具、读侧回读断言、
受控触发、删除保护校验、清理顺序与守卫；以及同步 DB 步骤
（账户资金配置 / 发布 / 运行实例清理）。

所有 MCP 调用都由 :class:`FakeMcpSession` 内存模拟，**不连网络**；
fake 的错误返回契约与真实服务一致（``is_error=True`` + 通用文本，
服务端异常详情只落在服务端日志）。
"""
# pylint: disable=protected-access  # 测试需访问私有成员以验证内部状态
import asyncio

from decimal import Decimal

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.models import AccountFundConfig, SuiteRun
from apps.plans.models import Plan
from apps.suites.models import Suite
from mcp_server.smoke import (
    REQUIRED_TOOLS, McpSmokeRunner, SmokeError, _decode, _is_error,
)


class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    """模拟 ``CallToolResult``（mcp 2.x：字段名为 ``is_error``）。"""

    def __init__(self, is_error, text):
        self.is_error = is_error
        self.content = [_Content(text)]


class _ToolFailure(RuntimeError):
    """fake 工具内部失败标记（对应服务端抛异常）。"""


class FakeMcpSession:
    """内存版 MCP 会话：模拟冒烟所用工具的语义（含删除保护与触发门禁）。

    Args:
        tools: 对外暴露的工具名列表，默认 = :data:`REQUIRED_TOOLS`。
        mutate: 配置写门禁是否开启（关闭时写工具返回错误）。
        trigger: 受控触发门禁是否开启（关闭时触发返回错误）。
    """

    def __init__(self, tools=None, mutate=True, trigger=True):
        self._tools = list(REQUIRED_TOOLS if tools is None else tools)
        self.mutate = mutate
        self.trigger = trigger
        self.cases: dict[int, dict] = {}
        self.suites: dict[int, dict] = {}
        self.plans: dict[int, dict] = {}
        self.runs: dict[int, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    async def list_tools(self):
        return list(self._tools)

    async def call(self, name, args=None):
        args = dict(args or {})
        self.calls.append((name, args))
        if name not in self._tools:
            return True, f'unknown tool {name}'
        handler = getattr(self, f'_tool_{name}', None)
        if handler is None:
            return True, f'Error executing tool {name}'
        if name.startswith(('create_', 'update_', 'delete_')) and not self.mutate:
            return True, f'Error executing tool {name}'
        try:
            return False, handler(args)
        except _ToolFailure:
            # 与真实服务一致：异常详情不外泄，客户端只拿到通用文本
            return True, f'Error executing tool {name}'

    # --- 写工具 ---
    def _tool_create_case(self, args):
        cid = self._next()
        self.cases[cid] = {
            'id': cid, 'name': args['name'], 'node_type': args['node_type'],
            'params': args.get('params') or {}, 'status': 'draft', 'version': 1,
        }
        return dict(self.cases[cid])

    def _tool_update_case(self, args):
        case = self.cases[args['case_id']]
        for key in ('name', 'node_type', 'params'):
            if args.get(key) is not None:
                case[key] = args[key]
        return dict(case)

    def _tool_delete_case(self, args):
        cid = args['case_id']
        if any(cid in suite['case_ids'] for suite in self.suites.values()):
            raise _ToolFailure('Case 已被 Suite 引用，不能删除')
        self.cases.pop(cid, None)
        return {'deleted': 'case', 'id': cid}

    def _tool_create_suite(self, args):
        sid = self._next()
        case_ids = list(args.get('case_ids') or [])
        self.suites[sid] = {
            'id': sid, 'name': args['name'], 'status': 'draft',
            'aggregate_method': args.get('aggregate_method', 'weighted_sum'),
            'cases': case_ids, 'case_ids': case_ids,
        }
        return dict(self.suites[sid])

    def _tool_update_suite(self, args):
        suite = self.suites[args['suite_id']]
        if args.get('case_ids') is not None:
            suite['case_ids'] = list(args['case_ids'])
            suite['cases'] = list(args['case_ids'])
        return dict(suite)

    def _tool_update_suite_topology(self, args):
        suite = self.suites[args['suite_id']]
        suite['case_ids'] = list(args['case_ids'])
        suite['cases'] = list(args['case_ids'])
        return {'topology_updated': suite['id']}

    def _tool_delete_suite(self, args):
        self.suites.pop(args['suite_id'], None)
        return {'deleted': 'suite', 'id': args['suite_id']}

    def _tool_create_plan(self, args):
        pid = self._next()
        self.plans[pid] = {
            'id': pid, 'name': args['name'], 'status': 'draft', 'version': 1,
            'root_suite_id': args['root_suite_id'],
        }
        return dict(self.plans[pid])

    def _tool_update_plan(self, args):
        plan = self.plans[args['plan_id']]
        plan.update({k: v for k, v in args.items() if k != 'plan_id' and v is not None})
        return dict(plan)

    def _tool_delete_plan(self, args):
        pid = args['plan_id']
        if any(run['plan_id'] == pid for run in self.runs.values()):
            raise _ToolFailure('Plan 已有执行记录')
        self.plans.pop(pid, None)
        return {'deleted': 'plan', 'id': pid}

    # --- 读工具 ---
    def _tool_list_plans(self, args):
        status = args.get('status')
        items = [
            {'id': p['id'], 'name': p['name'], 'status': p['status'], 'version': p['version']}
            for p in self.plans.values() if not status or p['status'] == status
        ]
        return {'count': len(items), 'plans': items}

    def _plan_symbol_codes(self, plan):
        """复刻真实解析：遍历根 Suite 的 Case，取 ``params.symbol_scope`` 的并集。"""
        suite = self.suites.get(plan.get('root_suite_id')) or {}
        codes = []
        for cid in suite.get('case_ids', []):
            params = (self.cases.get(cid) or {}).get('params') or {}
            scope = params.get('symbol_scope') or {}
            for code in scope.get('symbol_codes') or []:
                if code not in codes:
                    codes.append(code)
        return codes

    def _tool_get_plan(self, args):
        plan = self.plans.get(args['plan_id'])
        if plan is None:
            raise _ToolFailure('Plan 不存在')
        codes = self._plan_symbol_codes(plan)
        return {
            **plan, 'symbols': [{'code': code} for code in codes],
            'symbol_count': len(codes),
        }

    def _tool_list_cases(self, args):
        status = args.get('status')
        items = [
            {'id': c['id'], 'name': c['name'], 'node_type': c['node_type'],
             'status': c['status'], 'version': c['version']}
            for c in self.cases.values() if not status or c['status'] == status
        ]
        return {'count': len(items), 'cases': items}

    def _tool_get_case(self, args):
        case = self.cases.get(args['case_id'])
        if case is None:
            raise _ToolFailure('Case 不存在')
        return dict(case)

    def _tool_get_suite_topology(self, args):
        suite = self.suites.get(args['suite_id'])
        if suite is None:
            raise _ToolFailure('Suite 不存在')
        return {
            'id': suite['id'], 'name': suite['name'], 'status': suite['status'],
            'topology': {
                'suite_id': suite['id'], 'aggregate_method': suite['aggregate_method'],
                'case_ids': list(suite['case_ids']),
            },
        }

    def _tool_list_suite_runs(self, args):
        plan_id = args.get('plan_id') or 0
        items = [dict(run) for run in self.runs.values()
                 if not plan_id or run['plan_id'] == plan_id]
        return {'count': len(items), 'runs': items}

    # --- 受控触发 ---
    def _tool_trigger_plan_execution(self, args):
        if not self.trigger:
            raise _ToolFailure('写开关未开启')
        plan = self.plans.get(args['plan_id'])
        if plan is None or plan['status'] != 'published':
            raise _ToolFailure('Plan 必须已发布')
        symbols = list(args.get('symbols') or [])
        if not symbols:
            raise _ToolFailure('标的不能为空')
        run_ids = []
        for symbol in symbols:
            run_id = self._next()
            self.runs[run_id] = {
                'id': run_id, 'plan_id': plan['id'], 'status': 'pending', 'symbol': symbol,
            }
            run_ids.append(run_id)
        return {'plan_id': plan['id'], 'created_run_ids': run_ids, 'count': len(run_ids)}

    # --- 测试辅助 ---
    def publish_all(self, ids) -> None:
        """把 fake 中对应对象标记为 published（模拟 ORM 侧发布后的服务端视图）。"""
        for key in ('signal_case_id', 'executor_case_id'):
            if ids.get(key) in self.cases:
                self.cases[ids[key]]['status'] = 'published'
                self.cases[ids[key]]['version'] = 2
        if ids.get('suite_id') in self.suites:
            self.suites[ids['suite_id']]['status'] = 'published'
            self.suites[ids['suite_id']]['version'] = 2
        if ids.get('plan_id') in self.plans:
            self.plans[ids['plan_id']]['status'] = 'published'
            self.plans[ids['plan_id']]['version'] = 2


class ResultNormalisationTest(TestCase):
    """``CallToolResult`` 归一：错误标志与返回体解析。"""

    def test_is_error_reads_snake_case(self):
        self.assertTrue(_is_error(_Result(True, 'boom')))

    def test_is_error_reads_camel_case(self):
        class _Camel:
            isError = True

        self.assertTrue(_is_error(_Camel()))

    def test_is_error_defaults_false_when_attribute_absent(self):
        self.assertFalse(_is_error(object()))

    def test_decode_parses_json_and_falls_back_to_text(self):
        self.assertEqual(_decode(_Result(False, '{"a": 1}')), {'a': 1})
        self.assertEqual(_decode(_Result(True, 'Error executing tool x')),
                         'Error executing tool x')

    def test_decode_returns_none_for_empty_content(self):
        self.assertIsNone(_decode(type('_R', (), {'content': []})()))


class SmokeRunnerStepTest(TestCase):
    """异步 MCP 步骤：工具清单 / 建夹具 / 读侧校验 / 触发 / 删除保护 / 清理。"""

    def setUp(self):
        self.session = FakeMcpSession()
        self.runner = McpSmokeRunner(symbol_code='000426')

    # --- 辅助 ---
    def _create(self, runner=None):
        runner = runner or self.runner
        session = self.session

        async def flow():
            await runner.check_tools(session)
            await runner.create_fixture(session)

        asyncio.run(flow())
        return runner.ids

    def _verify(self, runner=None):
        runner = runner or self.runner
        session = self.session

        async def flow():
            return await runner.verify_reads(session)

        return asyncio.run(flow())

    # --- check_tools ---
    def test_check_tools_passes_when_all_required_present(self):
        names = asyncio.run(self.runner.check_tools(self.session))
        self.assertEqual(set(REQUIRED_TOOLS) - set(names), set())
        self.assertIn('check_tools', self.runner.steps)

    def test_check_tools_reports_missing_tools(self):
        session = FakeMcpSession(tools=sorted(REQUIRED_TOOLS - {'create_plan'}))
        with self.assertRaises(SmokeError) as ctx:
            asyncio.run(self.runner.check_tools(session))
        self.assertIn('create_plan', str(ctx.exception))

    # --- create_fixture ---
    def test_create_fixture_records_ids_and_call_order(self):
        ids = self._create()
        self.assertEqual(set(ids), {
            'signal_case_id', 'executor_case_id', 'suite_id', 'plan_id',
        })
        order = [name for name, _ in self.session.calls]
        self.assertEqual(order, [
            'create_case', 'create_case', 'create_suite',
            'update_suite_topology', 'create_plan',
        ])
        suite_call = self.session.calls[2][1]
        self.assertEqual(suite_call['case_ids'],
                         [ids['signal_case_id'], ids['executor_case_id']])
        topology_call = self.session.calls[3][1]
        self.assertEqual(topology_call['case_ids'], suite_call['case_ids'])
        self.assertEqual(topology_call['edges'], [])

    def test_create_fixture_forwards_account_and_declares_symbols_on_cases(self):
        runner = McpSmokeRunner(account_id='acc-1', allocated_capital=5000)
        self._create(runner)
        plan_args = [args for name, args in self.session.calls if name == 'create_plan'][0]
        self.assertEqual(plan_args['account_id'], 'acc-1')
        self.assertEqual(plan_args['allocated_capital'], 5000)
        # 标的范围不再随 Plan 下发，而是声明在 Case.params 上
        self.assertNotIn('symbol_scope', plan_args)
        for name, args in self.session.calls:
            if name == 'create_case':
                self.assertEqual(
                    args['params']['symbol_scope'],
                    {'type': 'symbols', 'symbol_codes': ['000426']})
        self.assertEqual(plan_args['trigger_type'], 'manual')
        self.assertEqual(plan_args['suite_start_mode'], 'manual')
        self.assertEqual(plan_args['exec_mode'], 'serial')

    def test_create_fixture_omits_account_when_not_configured(self):
        self._create()
        plan_args = [args for name, args in self.session.calls if name == 'create_plan'][0]
        self.assertNotIn('account_id', plan_args)
        self.assertNotIn('allocated_capital', plan_args)

    def test_create_fixture_raises_when_mutate_gate_closed(self):
        session = FakeMcpSession(mutate=False)
        with self.assertRaises(SmokeError) as ctx:
            asyncio.run(self.runner.create_fixture(session))
        self.assertIn('create_case', str(ctx.exception))

    # --- verify_reads ---
    def test_verify_reads_passes_on_consistent_state(self):
        ids = self._create()
        self.session.publish_all(ids)
        checks = self._verify()
        self.assertEqual(checks['symbols'], ['000426'])
        self.assertEqual(checks['symbol_count'], 1)
        self.assertEqual(checks['topology_case_ids'],
                         [ids['signal_case_id'], ids['executor_case_id']])
        self.assertEqual(checks['suite_runs'], 0)

    def test_verify_reads_detects_symbol_mismatch(self):
        ids = self._create()
        self.session.publish_all(ids)
        # 让 Case 不再声明标的 → 解析出 0 个，与期望的 1 个不符
        for cid in self.session.suites[ids['suite_id']]['case_ids']:
            params = dict(self.session.cases[cid]['params'])
            params.pop('symbol_scope', None)
            self.session.cases[cid]['params'] = params
        with self.assertRaises(SmokeError) as ctx:
            self._verify()
        self.assertIn('symbol_count', str(ctx.exception))

    def test_verify_reads_detects_topology_mismatch(self):
        ids = self._create()
        self.session.publish_all(ids)
        self.session.suites[ids['suite_id']]['case_ids'] = [ids['signal_case_id']]
        with self.assertRaises(SmokeError) as ctx:
            self._verify()
        self.assertIn('拓扑与写入不一致', str(ctx.exception))

    def test_verify_reads_requires_created_fixture(self):
        with self.assertRaises(SmokeError) as ctx:
            self._verify()
        self.assertIn('缺少主键', str(ctx.exception))


class SmokeTriggerAndCleanupTest(TestCase):
    """受控触发、删除保护校验与清理顺序。"""

    def setUp(self):
        self.session = FakeMcpSession()
        self.runner = McpSmokeRunner(symbol_code='000426', trigger=True)

        async def prepare():
            await self.runner.check_tools(self.session)
            await self.runner.create_fixture(self.session)

        asyncio.run(prepare())
        self.session.publish_all(self.runner.ids)

    # --- trigger_execution ---
    def test_trigger_execution_creates_pending_run(self):
        run_ids = asyncio.run(self.runner.trigger_execution(self.session))
        self.assertEqual(len(run_ids), 1)
        self.assertEqual(self.runner.created_run_ids, run_ids)
        self.assertEqual(self.session.runs[run_ids[0]]['status'], 'pending')
        self.assertEqual(self.session.runs[run_ids[0]]['symbol'], '000426')

    def test_trigger_execution_raises_when_gate_closed(self):
        self.session.trigger = False
        with self.assertRaises(SmokeError) as ctx:
            asyncio.run(self.runner.trigger_execution(self.session))
        self.assertIn('trigger_plan_execution', str(ctx.exception))

    # --- check_delete_protection ---
    def test_delete_protection_passes_for_referenced_case(self):
        asyncio.run(self.runner.check_delete_protection(self.session))
        self.assertIn('check_delete_protection', self.runner.steps)
        # 被引用时不应真的删除
        self.assertIn(self.runner.ids['signal_case_id'], self.session.cases)

    def test_delete_protection_detects_broken_protection(self):
        signal_id = self.runner.ids['signal_case_id']
        for suite in self.session.suites.values():
            suite['case_ids'] = [cid for cid in suite['case_ids'] if cid != signal_id]
        with self.assertRaises(SmokeError) as ctx:
            asyncio.run(self.runner.check_delete_protection(self.session))
        self.assertIn('删除保护失效', str(ctx.exception))

    # --- delete_fixture ---
    def test_delete_fixture_deletes_in_dependency_order(self):
        asyncio.run(self.runner.delete_fixture(self.session))
        deleted_calls = [name for name, _ in self.session.calls[-5:-1]]
        self.assertEqual(deleted_calls, [
            'delete_plan', 'delete_suite', 'delete_case', 'delete_case',
        ])
        self.assertEqual(self.session.cases, {})
        self.assertEqual(self.session.suites, {})
        self.assertEqual(self.session.plans, {})

    def test_delete_fixture_requires_runs_purged_first(self):
        asyncio.run(self.runner.trigger_execution(self.session))
        with self.assertRaises(SmokeError) as ctx:
            asyncio.run(self.runner.delete_fixture(self.session))
        self.assertIn('purge_runs', str(ctx.exception))

    def test_delete_fixture_detects_leftover_cases(self):
        def _noop_delete_case(args):
            return {'deleted': 'case', 'id': args['case_id']}

        self.session._tool_delete_case = _noop_delete_case  # noqa: SLF001
        with self.assertRaises(SmokeError) as ctx:
            asyncio.run(self.runner.delete_fixture(self.session))
        self.assertIn('清理后 Case 仍存在', str(ctx.exception))


class SmokeSyncStepsTest(TestCase):
    """同步 DB 步骤：账户资金配置 / 发布 / 运行实例清理。"""

    #: 标的范围由 Case 声明（否则 Plan 不得发布）
    SYMBOL_SCOPE = {'type': 'symbols', 'symbol_codes': ['000426']}

    SIGNAL_PARAMS = {
        'trigger': {'event_type': 'SUITE_INIT'},
        'indicator': 'rsi', 'direction': 1, 'period': 14,
        'threshold_oversold': 30, 'threshold_overbought': 70,
        'symbol_scope': SYMBOL_SCOPE,
    }
    ORDER = {'direction': 'buy', 'price': 12.5, 'volume': 100}
    EXECUTOR_PARAMS = {
        'trigger': {'event_type': 'CASE_COMPLETED'},
        'order': ORDER,
        'result': {'direction': 1, 'payload': {}, 'order': ORDER},
        'symbol_scope': SYMBOL_SCOPE,
    }

    def _draft_fixture(self):
        """ORM 直建 draft 夹具（等价于 MCP 写工具建出的状态）。"""
        signal = Case.objects.create(
            name='smoke signal', node_type='signal', params=self.SIGNAL_PARAMS)
        executor = Case.objects.create(
            name='smoke executor', node_type='executor', params=self.EXECUTOR_PARAMS)
        suite = Suite.objects.create(name='smoke suite', aggregate_method='weighted_sum')
        suite.cases.set([signal, executor])
        plan = Plan.objects.create(
            name='smoke plan', root_suite=suite, trigger_type='manual',
        )
        runner = McpSmokeRunner(symbol_code='000426')
        runner.ids = {
            'signal_case_id': signal.pk, 'executor_case_id': executor.pk,
            'suite_id': suite.pk, 'plan_id': plan.pk,
        }
        return runner, signal, executor, suite, plan

    # --- prepare_account ---
    def test_prepare_account_creates_once_and_is_idempotent(self):
        runner = McpSmokeRunner(account_id='smoke-acc')
        first = runner.prepare_account()
        second = runner.prepare_account()
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        # 字符串仅供展示（刚创建时可能是 '100000'、回读为 '100000.00'），按数值断言
        self.assertEqual(
            Decimal(first['total_capital']),
            AccountFundConfig.objects.get(account_id='smoke-acc').total_capital,
        )
        self.assertEqual(AccountFundConfig.objects.filter(account_id='smoke-acc').count(), 1)

    def test_prepare_account_noop_without_account_id(self):
        self.assertEqual(
            McpSmokeRunner().prepare_account(),
            {'account_id': '', 'created': False},
        )
        self.assertFalse(AccountFundConfig.objects.exists())

    # --- publish_fixture ---
    def test_publish_fixture_publishes_cases_suite_and_plan(self):
        runner, signal, executor, suite, plan = self._draft_fixture()
        result = runner.publish_fixture()
        signal.refresh_from_db()
        executor.refresh_from_db()
        suite.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(
            [signal.status, executor.status], ['published', 'published'])
        self.assertEqual([signal.version, executor.version], [2, 2])
        self.assertEqual(suite.status, 'published')
        self.assertEqual(plan.status, 'published')
        self.assertEqual(plan.versions.count(), 1)
        self.assertEqual([c['id'] for c in result['cases']], [signal.pk, executor.pk])
        self.assertIn('publish_fixture', runner.steps)

    def test_publish_fixture_is_idempotent_on_published_fixture(self):
        runner, _signal, _executor, _suite, plan = self._draft_fixture()
        runner.publish_fixture()
        runner.publish_fixture()
        plan.refresh_from_db()
        self.assertEqual(plan.version, 2)  # 不重复发布
        self.assertEqual(plan.versions.count(), 1)

    def test_publish_fixture_requires_created_fixture(self):
        with self.assertRaises(SmokeError):
            McpSmokeRunner().publish_fixture()

    # --- purge_runs ---
    def test_purge_runs_deletes_only_own_runs_and_is_idempotent(self):
        runner, _signal, _executor, suite, plan = self._draft_fixture()
        other_plan = Plan.objects.create(
            name='other', root_suite=suite, trigger_type='manual')
        mine = SuiteRun.objects.create(plan=plan, suite=suite, symbol='000426')
        other = SuiteRun.objects.create(plan=other_plan, suite=suite, symbol='600000')

        runner.created_run_ids = [mine.pk]
        self.assertEqual(runner.purge_runs(), 1)
        self.assertFalse(SuiteRun.objects.filter(pk=mine.pk).exists())
        self.assertTrue(SuiteRun.objects.filter(pk=other.pk).exists())
        # 幂等：第二次调用不再重复统计（返回首次结果，不把计数清零）
        self.assertEqual(runner.purge_runs(), 1)
        self.assertEqual(runner.purged_runs, 1)

    def test_purge_runs_noop_without_trigger(self):
        runner = McpSmokeRunner()
        self.assertEqual(runner.purge_runs(), 0)
        self.assertEqual(runner.steps, [])
