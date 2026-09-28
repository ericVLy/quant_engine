import asyncio
import logging

logger = logging.getLogger(__name__)


class TaskQueue:
    """Async FIFO queue of ``(plan, symbol, payload)`` tasks."""

    def __init__(self):
        self._queue = asyncio.Queue()

    async def put(self, plan, symbol, payload=None):
        await self._queue.put((plan, symbol, payload or {}))

    def put_nowait(self, plan, symbol, payload=None):
        self._queue.put_nowait((plan, symbol, payload or {}))

    async def get(self):
        return await self._queue.get()

    def task_done(self):
        self._queue.task_done()

    async def join(self):
        await self._queue.join()

    def empty(self):
        return self._queue.empty()


class WorkerPool:
    def __init__(self, runner, worker_count=1):
        if worker_count < 1:
            raise ValueError('worker_count 必须大于 0')
        self.runner = runner
        self.worker_count = worker_count

    async def _execute(self, plan, symbol, payload):
        """按 ``plan.retry_policy`` 执行单个任务（失败重试 + 退避）。

        Args:
            plan: 计划实例（需带 ``retry_policy``，可缺省为不重试）。
            symbol: 标的代码。
            payload: 触发载荷（透传给 ``runner.arun``）。

        Returns:
            Any: ``runner.arun`` 的返回值。

        Raises:
            ValueError: ``max_retries`` 为负数。
            Exception: 重试耗尽后抛出最后一次异常。
        """
        policy = getattr(plan, 'retry_policy', None) or {}
        max_retries = int(policy.get('max_retries', 0))
        if max_retries < 0:
            raise ValueError('max_retries 必须大于等于 0')
        for attempt in range(max_retries + 1):
            try:
                return await self.runner.arun(plan, symbol, payload)
            except Exception:
                if attempt >= max_retries:
                    raise
                delay = max(0, float(policy.get('delay_seconds', 0)))
                if delay:
                    await asyncio.sleep(delay)
        return None

    def _make_worker(self, task_queue, errors):
        """构造一个 worker 协程工厂：取任务 → 执行 → 记账 → 等待下一条。"""

        async def worker():
            while True:
                try:
                    plan, symbol, payload = await task_queue.get()
                except asyncio.CancelledError:
                    return
                try:
                    await self._execute(plan, symbol, payload)
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append(exc)
                finally:
                    task_queue.task_done()

        return worker

    async def run(self, task_queue):
        """排空队列后返回（一次性批处理语义：测试与单批任务用）。"""
        errors: list[Exception] = []
        workers = [asyncio.create_task(self._make_worker(task_queue, errors)())
                   for _ in range(self.worker_count)]
        await task_queue.join()
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if errors:
            raise errors[0]

    async def run_forever(self, task_queue, stop_event=None):
        """常驻消费：持续取任务执行，直到 ``stop_event`` 置位后退出。

        与 :meth:`run` 的两点区别：

        1. **不等待队列排空**——队列空闲时保持等待，因此可直接作为长驻进程的
           消费端（此前队列只进不出，时间驱动 Plan 永不执行）；
        2. **单任务失败只记日志不中断**——守护进程语义：某个策略失败（业务异常）
           不应拖垮整个调度器。失败详情已由 ``SuiteRun`` / ``ExecutionLog``
           落库，这里补一条可追踪的错误日志。

        Args:
            task_queue: 任务队列（与 Scheduler 共用同一个实例）。
            stop_event: ``asyncio.Event``；置位后取消所有 worker 并返回。
        """
        stop = stop_event or asyncio.Event()
        errors: list[Exception] = []
        workers = [asyncio.create_task(self._make_worker(task_queue, errors)())
                   for _ in range(self.worker_count)]
        try:
            await stop.wait()
        finally:
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        for exc in errors:
            logger.error('任务执行失败（已跳过，详情见 ExecutionLog）：%s', exc)
        if errors:
            logger.warning('本次常驻消费共 %d 个任务失败', len(errors))