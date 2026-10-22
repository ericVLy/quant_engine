"""分时更新器的**子进程隔离**与看门狗测试（2026-10-14）。

背景：采样经``GmSnapshotProvider`` 调gm SDK 的 ``api.history``，实测在
"可连接但不响应"时该调用**持有 GIL 一直阻塞**——同进程内的心跳线程与 asyncio
事件循环全部停摆，``join(timeout)`` / ``wait_for`` 的超时逻辑根本执行不到，
整个 Web 进程被冻结。故采样必须放在子进程里，由父进程看护。
"""
# pylint: disable=import-outside-toplevel,protected-access  # 延迟导入；测试需访问内部状态
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from apps.monitoring.updater import (
    DEFAULT_HEARTBEAT_PATH, IntradayUpdater, SubprocessIntradayUpdater,
)


class HeartbeatTest(TestCase):
    """心跳文件：每轮循环刷新，父进程据此判断子进程是否卡死。"""

    databases = ['default', 'kline']

    def setUp(self):
        self.hb = Path(tempfile.gettempdir()) / 'test-intraday-heartbeat'
        if self.hb.exists():
            self.hb.unlink()

    def tearDown(self):
        if self.hb.exists():
            self.hb.unlink()

    def test_touch_heartbeat_creates_file(self):
        updater = IntradayUpdater(heartbeat_path=self.hb)
        updater._touch_heartbeat()
        self.assertTrue(self.hb.exists())

    def test_loop_touches_heartbeat_each_round(self):
        """一轮循环结束后心跳应被更新（重活全部 mock 掉，只验证接线）。"""
        updater = IntradayUpdater(interval=0.01, heartbeat_path=self.hb)
        with patch.object(updater, '_startup_clear'), \
             patch.object(updater, '_maybe_backfill'), \
             patch.object(updater, 'run_once', return_value={}), \
             patch.object(updater, '_maybe_open_clear'), \
             patch.object(updater, '_maybe_cleanup'):
            updater.start()
            deadline = time.time() + 5
            while not self.hb.exists() and time.time() < deadline:
                time.sleep(0.05)
            updater.stop(timeout=5)
        self.assertTrue(self.hb.exists(), '心跳文件应在循环中被创建')
        first_mtime = self.hb.stat().st_mtime
        time.sleep(0.2)
        with patch.object(updater, '_startup_clear'), \
             patch.object(updater, '_maybe_backfill'), \
             patch.object(updater, 'run_once', return_value={}), \
             patch.object(updater, '_maybe_open_clear'), \
             patch.object(updater, '_maybe_cleanup'):
            updater.start()
            deadline = time.time() + 5
            while self.hb.stat().st_mtime == first_mtime and time.time() < deadline:
                time.sleep(0.05)
            updater.stop(timeout=5)
        self.assertGreater(self.hb.stat().st_mtime, first_mtime,
                           '心跳应每轮刷新')


class _FakeProc:
    """模拟一个"活着但可能卡住"的子进程。"""

    killed = False

    def __init__(self, returncode=None):
        self.pid = 4242
        self._rc = returncode

    def poll(self):
        return self._rc

    def kill(self):
        type(self).killed = True

    def wait(self, timeout=None):
        return 0


class SupervisorTest(TestCase):
    """守护线程：拉起 / 重启 / 看门狗。"""

    databases = ['default', 'kline']

    def setUp(self):
        _FakeProc.killed = False
        self.sup = SubprocessIntradayUpdater(interval=60)
        self.sup.heartbeat_path = DEFAULT_HEARTBEAT_PATH

    def _write_heartbeat(self, age_seconds):
        if DEFAULT_HEARTBEAT_PATH.exists():
            DEFAULT_HEARTBEAT_PATH.unlink()
        DEFAULT_HEARTBEAT_PATH.write_text('x')
        old = time.time() - age_seconds
        os.utime(DEFAULT_HEARTBEAT_PATH, (old, old))

    def test_spawn_disables_nested_updater(self):
        """子进程必须关掉内置更新器，否则会递归再拉一个子进程。"""
        with patch('apps.monitoring.updater.subprocess.Popen') as popen:
            self.sup._spawn()
        env = popen.call_args.kwargs['env']
        self.assertEqual(env['MONITORING_UPDATER_ENABLED'], '0',
                         '子进程须关闭内置更新器以避免递归拉起')
        cmd = popen.call_args.args[0]
        self.assertIn('run_intraday_updater', cmd)

    def test_respawns_when_child_exited(self):
        """子进程已退出时应重新拉起。"""
        self.sup._proc = _FakeProc(returncode=1)
        with patch('apps.monitoring.updater.subprocess.Popen') as popen:
            self.sup._supervise_once()
        popen.assert_called_once()

    def test_kills_child_when_heartbeat_stalled(self):
        """心跳停滞 → 强制 kill（应对子进程卡在 gm 调用里）。"""
        self._write_heartbeat(age_seconds=10_000)
        sup = SubprocessIntradayUpdater(interval=60, max_staleness=1)
        sup._proc = _FakeProc()
        sup._supervise_once()
        self.assertTrue(_FakeProc.killed, '心跳停滞应触发 kill')

    def test_keeps_child_when_heartbeat_fresh(self):
        """心跳新鲜 → 不动子进程。"""
        self._write_heartbeat(age_seconds=0)
        sup = SubprocessIntradayUpdater(interval=60, max_staleness=600)
        sup._proc = _FakeProc()
        sup._supervise_once()
        self.assertFalse(_FakeProc.killed, '心跳新鲜不应误杀')

    def test_is_running_reflects_child_state(self):
        self.sup._proc = _FakeProc(returncode=None)
        self.assertTrue(self.sup.is_running)
        self.sup._proc = _FakeProc(returncode=0)
        self.assertFalse(self.sup.is_running)

    def test_max_staleness_defaults_to_three_intervals(self):
        sup = SubprocessIntradayUpdater(interval=100)
        self.assertEqual(sup.max_staleness, 300)


class ProcessGateTest(TestCase):
    """``run_intraday_updater`` 自身不得再拉子进程（防递归）。"""

    databases = ['default', 'kline']

    def test_command_name_in_skip_list(self):
        """采样子进程命令不得再拉子进程（防递归）。

        回归（2026-10-22）：``ready()`` 的门禁从**黑名单**改为**白名单**
        （只有 Web 服务进程才采样）。本用例原先断言的是**源码里出现**
        ``'run_intraday_updater'`` 字面量（黑名单写法），白名单下该字面量
        已被移除——但**意图不变**：采样器命令仍不得拉起采样器。

        故改为断言**行为**（更稳，不依赖实现细节）。
        """
        from unittest.mock import patch

        from django.apps import apps as django_apps
        from django.conf import settings as django_settings

        app_config = django_apps.get_app_config('monitoring')
        with patch.object(django_settings, 'MONITORING_UPDATER_ENABLED', True), \
                patch('sys.argv', ['manage.py', 'run_intraday_updater']), \
                patch.dict(os.environ, {'RUN_MAIN': 'true'}, clear=False), \
                patch('apps.monitoring.updater.SubprocessIntradayUpdater') as supervisor:
            app_config.ready()
        supervisor.return_value.start.assert_not_called()
