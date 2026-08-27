import time
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.execution.events import EventType
from apps.execution.models import Event, ExecutionLog, Order
from apps.execution.services import create_suite_run, enqueue_event, start_suite_run

from .executor import CaseExecutionError, CaseExecutor


class EventLoop:
    def __init__(self, run, case_executor=None, context=None, broker=None):
        self.run = run
        self.case_executor = case_executor or CaseExecutor()
        self.context = context or {}
        self.broker = broker
        self.node_snapshots = {}
        self.direction = 0
        self.orders = []

    def _subscribed_cases(self, event):
        cases = self.run.suite.cases.filter(status='published')
        return [case for case in cases
                if (case.params or {}).get('trigger', {}).get('event_type') == event.event_type]

    def _create_order(self, log, order_data):
        required = {'direction', 'price', 'volume'}
        if not required.issubset(order_data):
            raise CaseExecutionError('order 必须包含 direction、price、volume')
        direction = order_data['direction']
        if direction not in ('buy', 'sell'):
            raise CaseExecutionError('order direction 必须是 buy 或 sell')
        order = Order.objects.create(
            log=log, symbol=self.run.symbol, direction=direction,
            price=Decimal(str(order_data['price'])), volume=int(order_data['volume']),
        )
        return order

    def run_to_completion(self):
        started = time.monotonic()
        if self.run.status == 'pending':
            start_suite_run(self.run)
            self.run.refresh_from_db()
        try:
            while self.run.event_queue:
                event = Event.objects.get(pk=self.run.event_queue[0], run=self.run)
                event.status = 'processing'
                event.save(update_fields=['status'])
                for case in self._subscribed_cases(event):
                    result = self.case_executor.execute(case, self.context)
                    self.direction = result.direction
                    self.context.update(result.payload)
                    self.node_snapshots[str(case.pk)] = result.payload
                    if result.order:
                        self.orders.append(result.order)
                    enqueue_event(self.run, EventType.CASE_COMPLETED,
                                  source=f'case:{case.pk}', payload=result.payload)
                event.status = 'done'
                event.processed_at = timezone.now()
                event.save(update_fields=['status', 'processed_at'])
                self.run.event_queue = self.run.event_queue[1:]
                self.run.save(update_fields=['event_queue'])
                self.run.refresh_from_db()

            log = ExecutionLog.objects.create(
                plan=self.run.plan, symbol=self.run.symbol,
                duration_ms=int((time.monotonic() - started) * 1000),
                final_direction=self.direction, node_snapshots=self.node_snapshots,
                status='success',
            )
            order_data = self.orders
            for item in order_data:
                order = self._create_order(log, item)
                if self.broker:
                    self.broker.submit_order(self.run.symbol, item)
                    order.status = 'sent'
                    order.save(update_fields=['status', 'updated_at'])
            self.run.status = 'completed'
            self.run.ended_at = timezone.now()
            self.run.save(update_fields=['status', 'ended_at'])
            return log
        except Exception as exc:
            self.run.status = 'failed'
            self.run.ended_at = timezone.now()
            self.run.save(update_fields=['status', 'ended_at'])
            ExecutionLog.objects.create(
                plan=self.run.plan, symbol=self.run.symbol,
                duration_ms=int((time.monotonic() - started) * 1000),
                final_direction=self.direction, node_snapshots=self.node_snapshots,
                status='failed', error_msg=str(exc),
            )
            raise


class SuiteRunner:
    def __init__(self, case_executor=None, broker=None):
        self.case_executor = case_executor
        self.broker = broker

    def run(self, plan, symbol, payload=None):
        run = create_suite_run(plan, symbol, payload)
        return EventLoop(run, self.case_executor, payload, self.broker).run_to_completion()

    async def arun(self, plan, symbol, payload=None):
        return await sync_to_async(self.run, thread_sensitive=True)(plan, symbol, payload)