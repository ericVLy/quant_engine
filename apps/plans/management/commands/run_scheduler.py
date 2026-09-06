from django.core.management.base import BaseCommand

from runner.scheduler import Scheduler


class Command(BaseCommand):
    help = '持续运行 Plan Cron 调度器'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=float,
            default=60,
            help='Cron 轮询间隔（秒），默认 60',
        )

    def handle(self, *args, **options):
        scheduler = Scheduler(poll_interval=options['interval'])
        self.stdout.write('Plan Cron scheduler started')
        try:
            scheduler.run_forever()
        except KeyboardInterrupt:
            scheduler.stop()
            self.stdout.write('Plan Cron scheduler stopped')