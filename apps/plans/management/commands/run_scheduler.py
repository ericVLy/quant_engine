"""持续运行 Plan Cron 调度器，并**消费** ``TaskQueue`` 执行策略。

与历史行为的差异：此前本命令只把到期任务投递到 ``TaskQueue``（只进不出），
没有任何进程消费，时间驱动 Plan 因此永远不会执行。现在默认同时启动
``--workers`` 个消费 worker，把任务真正跑到 ``SuiteRun`` / ``ExecutionLog`` /
``Order``（下单通道默认关闭，见 ``--order-broker``）。

用法：
    # 默认：2 个 worker 消费队列；委托单只落库，不真实下单
    python manage.py run_scheduler --interval 60

    # 真实下单（务必先确认账户/风控与模拟环境）
    python manage.py run_scheduler --order-broker gm

    # 退回历史行为：只投递不执行
    python manage.py run_scheduler --workers 0
"""
import asyncio
import signal

from django.core.management.base import BaseCommand, CommandError

from runner.scheduler import Scheduler


class Command(BaseCommand):
    help = ('持续运行 Plan Cron 调度器；--workers > 0 时同时消费 TaskQueue 执行策略'
            '（默认 2 个 worker）')

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=float,
            default=60,
            help='Cron 轮询间隔（秒），默认 60',
        )
        parser.add_argument(
            '--workers', type=int, default=2,
            help='消费 TaskQueue 的 worker 数量；0 = 只投递任务不执行（历史行为），默认 2',
        )
        parser.add_argument(
            '--order-broker', dest='order_broker', choices=('none', 'gm'), default='none',
            help='下单通道：none（默认，委托单只落库不提交）/ gm（经 gm SDK 真实下单）',
        )
        parser.add_argument(
            '--no-risk-control', dest='no_risk_control', action='store_true', default=False,
            help='不挂 RiskController（默认挂，做单笔/每日限额与交易时段校验）',
        )

    def handle(self, *args, **options):
        interval = options['interval']
        workers = options['workers']
        if workers < 0:
            raise CommandError('--workers 必须大于等于 0')
        scheduler = Scheduler(poll_interval=interval)
        self.stdout.write(
            f'Plan Cron scheduler started (interval={interval}s, workers={workers})'
        )
        if workers == 0:
            self.stdout.write('[scheduler] workers=0：仅投递任务到 TaskQueue，不执行策略')
            try:
                scheduler.run_forever()
            except KeyboardInterrupt:
                scheduler.stop()
            self.stdout.write('Plan Cron scheduler stopped')
            return
        asyncio.run(self._run_consuming(scheduler, workers, options))

    async def _run_consuming(self, scheduler, workers, options):
        """调度（生产任务）与消费（执行任务）在同一事件循环内并发运行。"""
        from runner.queue import WorkerPool
        from runner.service import build_execution_service

        service = build_execution_service(
            order_broker=options['order_broker'],
            enable_risk=not options['no_risk_control'],
        )
        pool = WorkerPool(service, worker_count=workers)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - 平台不支持信号处理
                pass

        self.stdout.write(f'[scheduler] 已启动 {workers} 个 worker 消费 TaskQueue')
        scheduler_task = asyncio.create_task(scheduler.run_forever_async(stop))
        pool_task = asyncio.create_task(pool.run_forever(scheduler.task_queue, stop))
        try:
            await asyncio.wait(
                [scheduler_task, pool_task], return_when=asyncio.FIRST_EXCEPTION
            )
            for task in (scheduler_task, pool_task):
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stop.set()
            for task in (scheduler_task, pool_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(scheduler_task, pool_task, return_exceptions=True)
            self.stdout.write('Plan Cron scheduler stopped')