"""分时监控采样命令。

用法：
  python manage.py sample_intraday [--markets=A,HK,US] [--symbols=000001,600519] [--interval=60]

- 缺省 ``--symbols``：取已发布 Plan 标的范围并集；没有已发布 Plan 时采样全部标的。
- 缺省 ``--interval=0`` 只执行一轮（供外部 cron / Windows 计划任务每分钟调用）；
  提供 ``--interval`` 可进入常驻轮询循环（对齐 ``run_scheduler`` 模式）。
- 非交易时段的市场自动跳过（``in_trading_session`` 判定），不影响交易中市场。
"""
import time

from django.core.management.base import BaseCommand

from apps.monitoring.services import sample_intraday


class Command(BaseCommand):
    help = '分时监控采样：按市场/标的拉取实时快照并写入 IntradayPoint'

    def add_arguments(self, parser):
        parser.add_argument(
            '--markets', type=str, default='',
            help='待采样市场，逗号分隔 A,HK,US；缺省取标的所在市场',
        )
        parser.add_argument(
            '--symbols', type=str, default='',
            help='待采样标的代码，逗号分隔；缺省取已发布 Plan 标的范围并集',
        )
        parser.add_argument(
            '--interval', type=float, default=0,
            help='常驻轮询间隔（秒）；0 表示只执行一轮（供外部 cron 调用）',
        )

    def handle(self, *args, **options):
        markets = [m.strip().upper() for m in options['markets'].split(',') if m.strip()]
        symbols = [s.strip() for s in options['symbols'].split(',') if s.strip()]
        interval = options['interval']

        while True:
            self.stdout.write(self.style.SUCCESS('[sample_intraday] 开始采样'))
            summary = sample_intraday(markets=markets, symbols=symbols)
            self._report(summary)
            if interval <= 0:
                return
            time.sleep(interval)

    def _report(self, summary):
        if not summary:
            self.stdout.write('  (无标的或全部标的处于非交易时段)')
            return
        for market, info in sorted(summary.items()):
            fallback = '采样'
            line = f'  [{market}] sampled={info["sampled"]} skipped={info["skipped"]}'
            if info['failed']:
                line += f' failed={len(info["failed"])}'
                fallback = '异常'
            styled = self.style.WARNING(line) if info['failed'] else getattr(self.style, 'SUCCESS', lambda x: x)(line)
            self.stdout.write(styled)