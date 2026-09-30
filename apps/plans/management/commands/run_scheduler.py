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
            '--funds-source', dest='funds_source', choices=('none', 'gm'), default='none',
            help='账户资金同步来源：none（默认，账户总资金由后台手工维护）'
                 '/ gm（按 gm 账户查询同步账户总资金与可用资金，只读账户不下单）',
        )
        parser.add_argument(
            '--funds-refresh-interval', dest='funds_refresh_interval', type=float, default=30,
            help='账户资金同步周期（秒，默认 30；<=0 表示仅在每次执行前同步）',
        )
        parser.add_argument(
            '--funds-capital-basis', dest='funds_capital_basis',
            choices=('total', 'cash', 'available'), default='total',
            help='额度上限口径：total（默认，账面资金+持仓市值）/ cash（只看账面资金，'
                 '账户存在本项目未管理的持仓时推荐）/ available（券商可用资金，最保守）',
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
        self._recover_orphans()
        self._reconcile_plan_states()
        if workers == 0:
            self.stdout.write('[scheduler] workers=0：仅投递任务到 TaskQueue，不执行策略')
            try:
                scheduler.run_forever()
            except KeyboardInterrupt:
                scheduler.stop()
            self.stdout.write('Plan Cron scheduler stopped')
            return
        asyncio.run(self._run_consuming(scheduler, workers, options))

    def _recover_orphans(self):
        """启动期收口上次进程遗留的未完成运行（P0 重启恢复）。

        崩溃（kill -9 / OOM / 断电）会在库里留下永久 ``running`` 的 ``SuiteRun``：
        既不告警，也不会被 ``apps/execution/retention.py`` 清理（它只处理终态）。
        这里在调度开始前收口一次（幂等），把「上次没跑完」显式变为一条失败记录
        + 一条 ``suite_failed`` 告警，避免静默失败。

        可用 ``EXECUTION_ORPHAN_RECOVERY_ENABLED=0`` 关闭；收口失败不阻断调度。
        同时按 ``EXECUTION_PENDING_MAX_AGE_SECONDS``（默认 300 秒）收口**过期**的
        ``pending`` 执行意向，避免陈旧交易意图被后续补投执行。
        """
        from django.conf import settings

        if not getattr(settings, 'EXECUTION_ORPHAN_RECOVERY_ENABLED', True):
            self.stdout.write(
                '[recovery] 已关闭（EXECUTION_ORPHAN_RECOVERY_ENABLED=0），跳过遗留运行收口')
            return None
        from apps.execution.recovery import recover_orphaned_runs

        try:
            stats = recover_orphaned_runs(
                pending_max_age=getattr(settings, 'EXECUTION_PENDING_MAX_AGE_SECONDS', None))
        except Exception as exc:  # pylint: disable=broad-except
            self.stderr.write(f'[recovery] 遗留运行收口失败（不阻断调度）：{exc}')
            return None
        if stats['runs']:
            self.stdout.write(
                f'[recovery] 已收口 {stats["runs"]} 个遗留未完成运行'
                f'（事件回退 {stats["events_reset"]}，节点收口 {stats["node_runs_failed"]}，'
                f'告警 {stats["alerts"]}）：{stats["run_ids"]}'
            )
        else:
            self.stdout.write('[recovery] 无遗留未完成运行')
        if stats['pending_expired']:
            self.stdout.write(
                f'[recovery] 已收口 {stats["pending_expired"]} 个过期的 pending 执行意向：'
                f'{stats["pending_expired_ids"]}'
            )
        if stats['pending_runs']:
            self.stdout.write(
                f'[recovery] 提示：{stats["pending_runs"]} 个 pending 执行意向仍在有效期内，'
                '将由消费循环认领执行')
        if stats['unconfirmed_orders']:
            self.stderr.write(
                f'[recovery] 警告：{stats["unconfirmed_orders"]} 张超时未确认委托单'
                f'（pending 且无外部订单号），需与券商对账：{stats["unconfirmed_order_ids"]}')
        return stats

    def _reconcile_plan_states(self):
        """启动期把 ``Plan.run_status`` 与执行事实（``SuiteRun``）对齐。

        修历史遗留的分叉：``run_status`` 原本只由 REST 手动动作流转，自动收口链条在
        生产链路零调用者，导致 Plan 长期停在 ``running``。归一只读 ``SuiteRun``、
        幂等无副作用；失败不阻断调度。
        """
        from apps.execution.state_sync import reconcile_all_plan_run_statuses

        try:
            stats = reconcile_all_plan_run_statuses()
        except Exception as exc:  # pylint: disable=broad-except
            self.stderr.write(f'[state-sync] Plan 状态归一失败（不阻断调度）：{exc}')
            return None
        if stats['changed_count']:
            self.stdout.write(
                f"[state-sync] 已归一 {stats['changed_count']} 个 Plan 的 run_status："
                f"{[(c['plan_id'], c['from'] + '→' + c['to']) for c in stats['changed']]}"
            )
        return stats

    async def _run_consuming(self, scheduler, workers, options):
        """调度（生产任务）与消费（执行任务）在同一事件循环内并发运行。"""
        from runner.queue import WorkerPool
        from runner.service import build_execution_service

        service = build_execution_service(
            order_broker=options['order_broker'],
            enable_risk=not options['no_risk_control'],
            funds_source=options['funds_source'],
            funds_refresh_interval=options['funds_refresh_interval'],
            funds_capital_basis=options['funds_capital_basis'],
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
        tasks = [
            asyncio.create_task(scheduler.run_forever_async(stop)),
            asyncio.create_task(pool.run_forever(scheduler.task_queue, stop)),
        ]
        if service.funds_broker is not None:
            interval = options['funds_refresh_interval']
            self.stdout.write(
                f'[scheduler] 账户资金同步已启用（周期 {interval}s；失败保留上次同步值）')
            tasks.append(asyncio.create_task(
                self._funds_loop(service.funds_broker, stop, interval,
                                 options['funds_capital_basis'])))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stop.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.stdout.write('Plan Cron scheduler stopped')

    async def _funds_loop(self, broker, stop, interval, capital_basis='total'):
        """周期性同步"已发布 Plan 引用账户"的资金（gm 账户查询）。

        与调度 / 消费同在一个事件循环：DB 与 gm 调用整体放在同步线程执行
        （``sync_to_async``），避免阻塞事件循环。任一账户失败只记日志、
        保留上次成功同步的数值（``fund_sync`` 契约）。
        """
        from asgiref.sync import sync_to_async

        from apps.execution.fund_sync import sync_published_plan_accounts

        period = interval if interval and interval > 0 else 30
        while not stop.is_set():
            try:
                results = await sync_to_async(sync_published_plan_accounts)(
                    broker, capital_basis=capital_basis)
                for item in results:
                    if 'error' in item:
                        self.stderr.write(f'[funds] 同步失败（保留上次值）: {item["error"]}')
                    else:
                        self.stdout.write(
                            f'[funds] 账户 {item["account_id"][:4]}… 已同步：'
                            f'额度={item["total_capital"]}（口径 {item["capital_basis"]}）'
                            f'可用={item["available_cash"]} 持仓市值={item["market_value"]}'
                        )
            except Exception as exc:  # pylint: disable=broad-except
                # 资金同步异常不得拖垮调度器
                self.stderr.write(f'[funds] 资金同步异常（下一轮重试）: {exc}')
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=period)
            except asyncio.TimeoutError:
                continue
