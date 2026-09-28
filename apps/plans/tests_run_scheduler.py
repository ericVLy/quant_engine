"""``manage.py run_scheduler`` 编排测试（消费端开关与参数透传）。"""
from io import StringIO
from unittest.mock import patch

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
            patcher = patch('apps.plans.management.commands.run_scheduler.asyncio.run')
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
