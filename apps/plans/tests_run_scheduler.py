"""``manage.py run_scheduler`` 编排测试（消费端开关、参数透传、资金同步循环）。"""
import asyncio
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.plans.management.commands.run_scheduler import Command
from runner.scheduler import Scheduler


class RunSchedulerCommandTest(SimpleTestCase):
    def _call(self, mock_run=True, **options):
        """执行命令。

        Args:
            mock_run: 为 ``True``（默认）时把 ``asyncio.run`` 替换成 mock，
                只断言"走了异步消费路径"而不真跑事件循环；为 ``False`` 时
                让 ``asyncio.run`` 真实执行（配合 ``_run_consuming`` 桩校验参数透传）。
            **options: 命令参数（未给出者用默认值）。

        Returns:
            tuple: ``(asyncio.run 的 mock 或 None, stdout 文本)``。
        """
        defaults = {
            'interval': 5, 'workers': 2, 'order_broker': 'none',
            'no_risk_control': False,
        }
        defaults.update(options)
        stdout = StringIO()
        run = None
        patcher = None
        if mock_run:
            def _fake_run(coro):
                # 只断言"走了异步消费路径"，不真正跑事件循环：关闭协程避免
                # "coroutine was never awaited" 警告
                coro.close()
                return MagicMock()

            patcher = patch('apps.plans.management.commands.run_scheduler.asyncio.run',
                            side_effect=_fake_run)
            run = patcher.start()
        try:
            call_command(Command(), stdout=stdout, stderr=StringIO(), **defaults)
        finally:
            if patcher is not None:
                patcher.stop()
        return run, stdout.getvalue()

    def test_default_workers_are_configured(self):
        run, out = self._call(workers=2)
        run.assert_called_once()      # 走异步"生产 + 消费"路径
        self.assertIn('workers=2', out)

    def test_workers_zero_uses_legacy_scheduler_only_loop(self):
        with patch.object(Scheduler, 'run_forever', autospec=True) as legacy:
            run, out = self._call(workers=0)
        run.assert_not_called()
        legacy.assert_called_once()   # 只投递、不执行
        self.assertIn('workers=0', out)
        self.assertIn('不执行策略', out)

    def test_options_forwarded_to_consuming_loop(self):
        captured = {}

        async def fake_consuming(self, scheduler, workers, options):
            captured['workers'] = workers
            captured['options'] = options

        with patch.object(Command, '_run_consuming', fake_consuming):
            self._call(mock_run=False, interval=7, workers=3,
                       order_broker='gm', no_risk_control=True)
        self.assertEqual(captured['workers'], 3)
        self.assertEqual(captured['options']['order_broker'], 'gm')
        self.assertTrue(captured['options']['no_risk_control'])

    def test_negative_workers_rejected(self):
        with self.assertRaisesRegex(CommandError, 'workers'):
            self._call(workers=-1)

    def test_default_order_broker_is_none(self):
        captured = {}

        async def fake_consuming(self, scheduler, workers, options):
            captured.update(options)

        with patch.object(Command, '_run_consuming', fake_consuming):
            self._call(mock_run=False)
        self.assertEqual(captured['order_broker'], 'none')
        self.assertFalse(captured['no_risk_control'])

    def test_funds_flags_are_forwarded_and_default_to_none(self):
        captured = {}

        async def fake_consuming(self, scheduler, workers, options):
            captured.update(options)

        with patch.object(Command, '_run_consuming', fake_consuming):
            self._call(mock_run=False, funds_source='gm', funds_refresh_interval=15)
        self.assertEqual(captured['funds_source'], 'gm')
        self.assertEqual(captured['funds_refresh_interval'], 15)

        defaults = {}
        with patch.object(Command, '_run_consuming',
                          lambda self, s, w, o: defaults.update(o) or asyncio.sleep(0)):
            self._call(mock_run=False)
        self.assertEqual(defaults['funds_source'], 'none')
        self.assertEqual(defaults['funds_refresh_interval'], 30)


class RunSchedulerFundsLoopTest(SimpleTestCase):
    """``_funds_loop``：周期性资金同步（失败不拖垮调度器）。"""

    def _run_loop(self, sync, period=0.01, basis='total'):
        from apps.execution import fund_sync

        out, err = StringIO(), StringIO()
        command = Command(stdout=out, stderr=err)

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(command._funds_loop('broker', stop, period, basis))
            for _ in range(200):
                await asyncio.sleep(0.005)
                if sync.calls:
                    break
            stop.set()
            await asyncio.wait_for(task, timeout=2)

        with patch.object(fund_sync, 'sync_published_plan_accounts', sync):
            asyncio.run(scenario())
        return out.getvalue(), err.getvalue()

    def test_loop_syncs_periodically_and_reports(self):
        class _Sync:
            def __init__(self):
                self.calls = []

            def __call__(self, broker, source='gm', capital_basis='total'):
                self.calls.append((broker, capital_basis))
                return [{'account_id': 'efd9…', 'total_assets': '100000',
                         'total_capital': '100000.00', 'capital_basis': capital_basis,
                         'computed_capital': '100000.00', 'allocated_capital': '0',
                         'clamped': False, 'available_cash': '90000',
                         'market_value': '10000'}]

        sync = _Sync()
        out, _ = self._run_loop(sync)
        self.assertTrue(sync.calls)
        self.assertEqual(sync.calls[0][1], 'total')
        self.assertIn('额度=100000.00', out)
        self.assertIn('口径 total', out)
        self.assertNotIn('efd94fdb', out)      # 账户 ID 不明文输出

    def test_loop_forwards_capital_basis(self):
        class _Sync:
            def __init__(self):
                self.calls = []

            def __call__(self, broker, source='gm', capital_basis='total'):
                self.calls.append(capital_basis)
                return []

        sync = _Sync()
        self._run_loop(sync, basis='cash')
        self.assertEqual(sync.calls, ['cash'])

    def test_loop_error_is_logged_and_loop_survives(self):
        class _Sync:
            def __init__(self):
                self.calls = []

            def __call__(self, broker, source='gm', capital_basis='total'):
                self.calls.append(broker)
                raise RuntimeError('terminal down')

        sync = _Sync()
        out, err = self._run_loop(sync)
        self.assertTrue(sync.calls)
        self.assertIn('资金同步异常', err)
        self.assertEqual(out, '')
