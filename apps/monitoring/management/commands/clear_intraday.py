"""分时监控清理命令。

用法：
  python manage.py clear_intraday [--before=YYYY-MM-DD]

- 删除 ``ts < --before`` 的全部记录；缺省 ``--before`` 为当日 00:00 UTC。
- 幂等：重复执行不报错（满足条件的记录不存在时删除 0 条）。
- 建议挂在外部 cron 于 UTC 23:00 触发（美股收盘后、A股开盘前，全市场当日数据同时过期）。
"""
from datetime import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.monitoring.services import clear_intraday


class Command(BaseCommand):
    help = '分时监控清理：删除指定时间之前的 IntradayPoint'

    def add_arguments(self, parser):
        parser.add_argument(
            '--before', type=str, default='',
            help='删除 ts 早于此日期的记录（YYYY-MM-DD）；缺省当日 00:00 UTC',
        )

    def handle(self, *args, **options):
        if options['before']:
            before = timezone.make_aware(
                datetime.strptime(options['before'], '%Y-%m-%d'), timezone.utc,
            )
        else:
            before = None
        deleted = clear_intraday(before=before)
        self.stdout.write(self.style.SUCCESS(f'清理完成，删除 {deleted} 条分时记录'))