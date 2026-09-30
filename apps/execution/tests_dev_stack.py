"""run_dev_stack 一键启动命令的编排契约测试（子进程全部 mock，不真正拉起服务）。"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from apps.execution.management.commands import run_dev_stack as cmd_mod
from apps.execution.management.commands.run_dev_stack import Command


class _FakeProc:
    def __init__(self, cmd, env=None):
        self.args = cmd
        self.env = env
        self.calls = []
        self._poll = 0  # 立即返回 0，驱动 fail-fast 分支

    def poll(self):
        return self._poll

    def terminate(self):
        self.calls.append('terminate')

    def kill(self):
        self.calls.append('kill')

    def wait(self, timeout=None):
        self.calls.append('wait')
        return 0


class RunDevStackCommandTest(SimpleTestCase):
    def _run(self, opts):
        procs = []

        def fake_popen(cmd, env=None):
            procs.append(_FakeProc(cmd, env))
            return procs[-1]

        with patch.object(cmd_mod.subprocess, 'Popen', side_effect=fake_popen):
            # 命令会向 stdout 打印 [dev-stack] 启动/停止行；测试内吞掉，保持回归日志干净
            call_command(Command(), stdout=StringIO(), stderr=StringIO(), **opts)
        return procs

    def test_starts_django_and_mcp_by_default(self):
        procs = self._run({})
        self.assertEqual(len(procs), 2)
        # 上一行已断言元素个数；pylint 只能看到辅助函数内的 `procs = []`，故报误报
        web, mcp = procs  # pylint: disable=unbalanced-tuple-unpacking
        self.assertIn('runserver', web.args)
        self.assertEqual(web.env.get('RUN_MAIN'), 'true')
        self.assertIn('--noreload', web.args)
        self.assertIn('run_mcp_server', mcp.args)
        self.assertNotIn('--allow-trigger', mcp.args)
        self.assertNotIn('--allow-mutate', mcp.args)
        # 退出前对所有子进程执行 wait 清理（poll 已返回的不再 terminate）
        for proc in procs:
            self.assertIn('wait', proc.calls)

    def test_options_forwarded_and_optional_scheduler(self):
        procs = self._run({
            'web_port': 8001, 'mcp_port': 8766, 'mcp_host': '0.0.0.0',
            'mcp_auth_token': 'tok', 'allow_trigger': True,
            'allow_mutate': True, 'with_scheduler': True,
        })
        self.assertEqual(len(procs), 3)
        web, mcp, sched = procs  # pylint: disable=unbalanced-tuple-unpacking  # 同上
        self.assertIn('127.0.0.1:8001', ' '.join(web.args))
        mcp_joined = ' '.join(mcp.args)
        self.assertIn('--port 8766', mcp_joined)
        self.assertIn('--auth-token tok', mcp_joined)
        self.assertIn('--allow-trigger', mcp_joined)
        self.assertIn('--allow-mutate', mcp_joined)
        self.assertIn('run_scheduler', ' '.join(sched.args))

    def test_namespaces_equality_with_default_ports(self):
        """默认端口契约：Django 8000 / MCP 8765。"""
        procs = self._run({})
        self.assertIn('127.0.0.1:8000', ' '.join(procs[0].args))
        self.assertIn('--port', ' '.join(procs[1].args))
        self.assertIn('8765', ' '.join(procs[1].args))

    def test_scheduler_worker_and_broker_are_forwarded(self):
        """调度器消费参数透传：worker 数与下单通道。"""
        procs = self._run({
            'with_scheduler': True, 'scheduler_workers': 4,
            'scheduler_order_broker': 'gm',
        })
        sched = ' '.join(procs[2].args)
        self.assertIn('run_scheduler', sched)
        self.assertIn('--workers 4', sched)
        self.assertIn('--order-broker gm', sched)

    def test_scheduler_defaults_are_record_only(self):
        """默认不真实下单（order_broker=none）。"""
        procs = self._run({'with_scheduler': True})
        self.assertIn('--order-broker none', ' '.join(procs[2].args))
        self.assertIn('--workers 2', ' '.join(procs[2].args))
