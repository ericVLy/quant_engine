import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.execution.events import EventType
from apps.execution.models import Event, ExecutionLog, Order
from apps.execution.services import create_suite_run, enqueue_event, start_suite_run
from apps.suites.services import aggregate_directions, event_condition_matches

from .executor import CaseExecutionError, CaseExecutor
from .risk import RiskController


class EventLoop:
    def __init__(self, run, case_executor=None, context=None, broker=None,
                 risk_controller=None):
        self.run = run
        self.case_executor = case_executor or CaseExecutor()
        self.context = context or {}
        self.broker = broker
        self.risk_controller = risk_controller
        self.node_snapshots = {}
        self.direction = 0
        self.orders = []

    def _subscribed_cases(self, event):
        cases = self.run.suite.cases.filter(status='published')
        return [case for case in cases
                if (case.params or {}).get('trigger', {}).get('event_type') == event.event_type]

    def _execute_cases(self, cases):
        if self.run.plan.exec_mode == 'parallel' and len(cases) > 1:
            with ThreadPoolExecutor(max_workers=len(cases)) as pool:
                return list(pool.map(
                    lambda case: self.case_executor.execute(case, self.context), cases
                ))
        results = []
        for case in cases:
            results.append(self.case_executor.execute(case, self.context))
        return results

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
                self.context.update(event.payload or {})
                cases = self._subscribed_cases(event)
                results = self._execute_cases(cases)
                aggregate_results = []
                for case, result in zip(cases, results):
                    self.direction = result.direction
                    self.context.update(result.payload)
                    self.node_snapshots[str(case.pk)] = result.payload
                    aggregate_results.append({
                        'direction': result.direction,
                        'weight': (case.params or {}).get('weight', 1.0),
                    })
                    if result.order:
                        self.orders.append(result.order)
                    enqueue_event(self.run, EventType.CASE_COMPLETED,
                                  source=f'case:{case.pk}', payload=result.payload)
                if aggregate_results:
                    self.direction = aggregate_directions(
                        self.run.suite, aggregate_results
                    )
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
                if self.risk_controller:
                    decision = self.risk_controller.check(item)
                    if not decision.allowed:
                        log.status = 'blocked'
                        log.error_msg = decision.reason
                        log.save(update_fields=['status', 'error_msg'])
                        self.run.status = 'failed'
                        self.run.ended_at = timezone.now()
                        self.run.save(update_fields=['status', 'ended_at'])
                        return log
                if self.broker:
                    response = self.broker.submit_order(self.run.symbol, item)
                    external_id = self._external_order_id(response)
                    if external_id:
                        order.external_order_id = external_id
                    order.status = 'sent'
                    order.save(update_fields=['status', 'external_order_id', 'updated_at'])
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

    @staticmethod
    def _external_order_id(response):
        if isinstance(response, dict):
            return response.get('cl_ord_id') or response.get('order_id')
        if isinstance(response, (list, tuple)) and response and isinstance(response[0], dict):
            return response[0].get('cl_ord_id') or response[0].get('order_id')
        return getattr(response, 'cl_ord_id', None) or getattr(response, 'order_id', None)


class SuiteRunner:
    def __init__(self, case_executor=None, broker=None, risk_controller=None):
        self.case_executor = case_executor
        self.broker = broker
        self.risk_controller = risk_controller

    def run(self, plan, symbol, payload=None):
        run = create_suite_run(plan, symbol, payload)
        return EventLoop(
            run, self.case_executor, payload, self.broker, self.risk_controller
        ).run_to_completion()

    async def arun(self, plan, symbol, payload=None):
        return await sync_to_async(self.run, thread_sensitive=True)(plan, symbol, payload)