"""清理过期执行日志与运行痕迹（N-04）。

用法::

    .\\.venv\\Scripts\\python.exe .\\manage.py purge_execution_logs --dry-run
    .\\.venv\\Scripts\\python.exe .\\manage.py purge_execution_logs --days 30

- 默认保留天数取 `settings.EXECUTION_LOG_RETENTION_DAYS`（30）；
- 仅清理终态运行的 Event / NodeRun 与**无订单**的 ExecutionLog；
- Order / Alert / SuiteRun / FundAllocation 一律保留（见 `retention.py` 表）。
"""
from django.core.management.base import BaseCommand, CommandError

from apps.execution.retention import RetentionError, purge_execution_history


class Command(BaseCommand):
    help = '清理过期执行痕迹（N-04：默认保留 30 天；订单与告警不受影响）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='保留天数，默认取 EXECUTION_LOG_RETENTION_DAYS（30）',
        )
        parser.add_argument('--dry-run', action='store_true', help='只统计不删除')

    def handle(self, *args, **options):
        try:
            stats = purge_execution_history(
                days=options.get('days'),
                dry_run=bool(options.get('dry_run')),
            )
        except RetentionError as exc:
            raise CommandError(str(exc)) from exc

        prefix = '[dry-run] ' if stats['dry_run'] else ''
        self.stdout.write(
            f"{prefix}截止时间 {stats['cutoff']}（保留 {stats['days']} 天）\n"
            f"{prefix}终态运行 {stats['runs_scanned']} 个："
            f"事件 {stats['events']} · 节点轨迹 {stats['node_runs']} · 执行日志 {stats['logs']}\n"
            f"{prefix}保留：含订单日志 {stats['logs_kept_with_orders']} 条"
            '（订单/告警/资金占用/SuiteRun 本体一律保留）'
        )
        if not stats['dry_run']:
            self.stdout.write(self.style.SUCCESS('清理完成（幂等，可重复执行）'))