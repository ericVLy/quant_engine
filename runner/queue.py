import asyncio


class TaskQueue:
    """Async FIFO queue of ``(plan, symbol, payload)`` tasks."""

    def __init__(self):
        self._queue = asyncio.Queue()

    async def put(self, plan, symbol, payload=None):
        await self._queue.put((plan, symbol, payload or {}))

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

    async def run(self, task_queue):
        async def worker():
            while True:
                try:
                    plan, symbol, payload = await task_queue.get()
                except asyncio.CancelledError:
                    return
                try:
                    await self.runner.arun(plan, symbol, payload)
                finally:
                    task_queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self.worker_count)]
        await task_queue.join()
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)