"""``manage.py mcp_smoke_test`` 命令测试（编排层）。

分工：
    - 本文件用全内存的 :class:`~mcp_server.tests_smoke.FakeMcpSession` + 同步 DB 步骤桩，
      验证**命令编排**：阶段顺序、参数透传、清理、`--keep` / `--no-publish` /
      `--trigger` 开关、失败上报与尽力清理；
    - 各步骤的真实语义（MCP 工具调用）由 ``mcp_server/tests_smoke.py`` 覆盖；
    - 真实 ORM 语义（发布 / 账户资金配置 / 运行实例清理）同样由
      ``mcp_server/tests_smoke.py::SmokeSyncStepsTest`` 覆盖。

之所以把 DB 步骤桩掉：命令用 ``asyncio.run`` 驱动 MCP 阶段，而 Django 的 SQLite
测试库是 shared-cache 内存库，在事件循环里直连 ORM 会与 TestCase 事务互锁
（``database table is locked``）。生产路径不存在该问题——ORM 只出现在同步阶段。
SSE 传输层由 ``mcp_server/tests.py`` 与实机冒烟运行覆盖。
"""
# pylint: disable=import-outside-toplevel,protected-access  # 延迟导入以规避循环依赖/加载期副作用；测试需访问私有成员以验证内部状态
import json
import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from mcp_server.smoke import McpSmokeRunner
from mcp_server.tests_smoke import FakeMcpSession

#: 测试用环境：放开两个 MCP 写门禁。
TEST_ENV = {'MCP_ALLOW_MUTATE': '1', 'MCP_ALLOW_TRIGGER': '1'}
#: 关闭配置写门禁（失败路径用例）。
GATES_CLOSED = {'MCP_ALLOW_MUTATE': '0', 'MCP_ALLOW_TRIGGER': '0'}


class _SessionFactory:
    """把 ``SseMcpSession`` 换成返回同一个内存会话的异步上下文管理器。"""

    def __init__(self, session):
        self.session = session

    def __call__(self, url, headers=None):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc_info):
        return False


def _db_step_stubs(session):
    """把三个同步 DB 步骤替换为内存桩（真实语义见 tests_smoke.SmokeSyncStepsTest）。"""

    def prepare_account(runner):
        runner.steps.append('prepare_account')
        return {'account_id': runner.account_id, 'created': True, 'total_capital': '100000'}

    def publish_fixture(runner):
        session.publish_all(runner.ids)   # 让内存会话反映"已发布"
        runner.steps.append('publish_fixture')
        return {'cases': [], 'suite': {}, 'plan': {}}

    def purge_runs(runner):
        for run_id in runner.created_run_ids:
            session.runs.pop(run_id, None)   # 模拟真实删库，解除 Plan 删除保护
        runner.purged_runs = len(runner.created_run_ids)
        runner._runs_purged = True        # noqa: SLF001
        runner.steps.append('purge_runs')
        return runner.purged_runs

    return (
        patch.object(McpSmokeRunner, 'prepare_account', prepare_account),
        patch.object(McpSmokeRunner, 'publish_fixture', publish_fixture),
        patch.object(McpSmokeRunner, 'purge_runs', purge_runs),
    )


class McpSmokeTestCommandTest(TestCase):
    """命令编排：阶段顺序、清理、失败处理与参数开关。"""

    def setUp(self):
        self.session = FakeMcpSession()
        self.stdout = StringIO()
        self.stderr = StringIO()

    def _run(self, session=None, env=None, **options):
        session = session or self.session
        defaults = {
            'url': None, 'auth_token': None, 'account_id': '',
            'allocated_capital': None, 'symbol': '000426',
            'publish': True, 'trigger': False, 'keep': False,
        }
        defaults.update(options)
        patches = [
            patch.dict(os.environ, env or TEST_ENV),
            patch('mcp_server.smoke.SseMcpSession', _SessionFactory(session)),
            *_db_step_stubs(session),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        call_command('mcp_smoke_test', stdout=self.stdout, stderr=self.stderr, **defaults)
        return session, self.stdout.getvalue(), self.stderr.getvalue()

    @staticmethod
    def _called(session, name):
        """返回指定工具的调用参数列表（按调用顺序）。"""
        return [args for call_name, args in session.calls if call_name == name]

    # --- 全链路 ---
    def test_full_pipeline_creates_publishes_verifies_and_cleans(self):
        session, out, _ = self._run(account_id='smoke-acc', allocated_capital=5000,
                                    trigger=True)
        names = [name for name, _ in session.calls]
        for expected in ('create_case', 'create_suite', 'update_suite_topology',
                         'create_plan', 'list_plans', 'get_plan', 'list_cases',
                         'get_case', 'get_suite_topology', 'list_suite_runs',
                         'trigger_plan_execution', 'delete_plan', 'delete_suite',
                         'delete_case'):
            self.assertIn(expected, names)
        self.assertIn('全部步骤通过', out)
        summary = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(summary['steps'], [
            'prepare_account', 'check_tools', 'create_fixture', 'publish_fixture',
            'verify_reads', 'trigger_execution', 'check_delete_protection',
            'purge_runs', 'delete_fixture',
        ])
        self.assertEqual(len(summary['created_run_ids']), 1)
        self.assertFalse(summary['kept'])
        # 夹具与会话内的运行实例都已清理
        self.assertEqual(session.cases, {})
        self.assertEqual(session.suites, {})
        self.assertEqual(session.plans, {})

    # --- 参数透传与开关 ---
    def test_account_id_without_capital_still_binds_account(self):
        session, _, _ = self._run(account_id='smoke-acc')
        plan_args = self._called(session, 'create_plan')[0]
        self.assertEqual(plan_args['account_id'], 'smoke-acc')
        self.assertNotIn('allocated_capital', plan_args)

    def test_allocated_capital_forwarded_when_provided(self):
        session, _, _ = self._run(account_id='smoke-acc', allocated_capital=5000)
        plan_args = self._called(session, 'create_plan')[0]
        self.assertEqual(plan_args['allocated_capital'], 5000)

    def test_symbol_flag_flows_into_case_symbol_scope(self):
        """--symbol 现在下发到 Case.params.symbol_scope（Plan 不再持有该字段）。"""
        session, _, _ = self._run(symbol='000001')
        plan_args = self._called(session, 'create_plan')[0]
        self.assertNotIn('symbol_scope', plan_args)
        case_args = [args for name, args in session.calls if name == 'create_case']
        self.assertTrue(case_args)
        for args in case_args:
            self.assertEqual(args['params']['symbol_scope'],
                             {'type': 'symbols', 'symbol_codes': ['000001']})
        self.assertEqual(self._called(session, 'trigger_plan_execution'), [])

    def test_trigger_flag_off_skips_trigger_tool(self):
        session, _, _ = self._run(account_id='smoke-acc')
        self.assertEqual(self._called(session, 'trigger_plan_execution'), [])
        self.assertEqual(session.runs, {})

    def test_keep_flag_skips_cleanup(self):
        session, out, _ = self._run(account_id='smoke-acc', keep=True)
        self.assertEqual(self._called(session, 'delete_plan'), [])
        self.assertEqual(self._called(session, 'delete_suite'), [])
        summary = json.loads(out.strip().splitlines()[-1])
        # 唯一的 delete_case 调用来自删除保护探测（被引用时必须被拒绝），不是清理
        self.assertEqual(
            self._called(session, 'delete_case'),
            [{'case_id': summary['created']['signal_case_id']}],
        )
        self.assertTrue(summary['kept'])
        self.assertTrue(session.cases and session.plans)   # 夹具保留

    def test_no_publish_skips_publish_and_published_listing(self):
        session, _, _ = self._run(account_id='smoke-acc', publish=False, keep=True)
        self.assertEqual(self._called(session, 'list_plans'), [])
        # 未发布：夹具仍为 draft
        self.assertTrue(all(c['status'] == 'draft' for c in session.cases.values()))

    # --- 失败路径 ---
    def test_trigger_failure_reports_ids_and_cleans_up(self):
        session = FakeMcpSession(trigger=False)      # 触发门禁关闭
        with self.assertRaises(CommandError) as ctx:
            self._run(session=session, trigger=True)
        self.assertIn('trigger_plan_execution', str(ctx.exception))
        self.assertIn('已创建主键', self.stderr.getvalue())
        # 尽力清理：夹具已删除
        self.assertEqual(session.cases, {})
        self.assertEqual(session.plans, {})

    def test_mutate_gate_closed_reports_failure(self):
        session = FakeMcpSession(mutate=False)
        with self.assertRaises(CommandError) as ctx:
            self._run(session=session, env=GATES_CLOSED, account_id='smoke-acc')
        self.assertIn('create_case', str(ctx.exception))
        self.assertIn('失败于步骤', self.stderr.getvalue())
        self.assertEqual(session.cases, {})

    # --- 参数 ---
    def test_default_url_follows_mcp_host_port_env(self):
        from apps.execution.management.commands.mcp_smoke_test import Command

        with patch.dict(os.environ, {'MCP_HOST': '10.0.0.5', 'MCP_PORT': '9999'}):
            self.assertEqual(Command._default_url(), 'http://10.0.0.5:9999/sse')

    def test_auth_token_is_sent_as_bearer_header(self):
        captured = {}

        class _CapturingFactory(_SessionFactory):
            def __call__(self, url, headers=None):
                captured['url'] = url
                captured['headers'] = headers
                return self

        patches = [
            patch.dict(os.environ, TEST_ENV),
            patch('mcp_server.smoke.SseMcpSession', _CapturingFactory(self.session)),
            *_db_step_stubs(self.session),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        call_command('mcp_smoke_test', stdout=self.stdout, stderr=self.stderr,
                     url='http://example/sse', auth_token='tok-1', account_id='',
                     allocated_capital=None, symbol='000426',
                     publish=False, trigger=False, keep=True)
        self.assertEqual(captured['url'], 'http://example/sse')
        self.assertEqual(captured['headers'], {'Authorization': 'Bearer tok-1'})
