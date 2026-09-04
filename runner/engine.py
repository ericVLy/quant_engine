import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.cases.models import Case
from apps.execution.events import EventType
from apps.execution.models import Event, ExecutionLog, NodeRun, Order
from apps.execution.services import create_suite_run, enqueue_event, start_suite_run
from apps.suites.models import Suite, SuiteVersion
from apps.suites.services import aggregate_directions, build_topology_snapshot, event_condition_matches

from .executor import CaseExecutionError, CaseExecutor
from .risk import RiskController


class EventLoop:
    def __init__(self, run, case_executor=None, context=None, broker=None,
                 risk_controller=None, context_builder=None, use_threads=True, **kwargs):
        self.run = run
        self.case_executor = case_executor or CaseExecutor()
        self.context = context or {}
        self.broker = broker
        self.risk_controller = risk_controller
        self.context_builder = context_builder
        self.use_threads = use_threads
        self.node_snapshots = {}
        self.direction = 0
        self.orders = []
        self.nodes = {}
        self._active_node = None
        self._fired_edges = set()

    # ------------------------------------------------------------------
    # 拓扑解析：优先使用发布快照（SuiteVersion），否则回退实时构建
    # ------------------------------------------------------------------
    def _load_topology(self):
        root = self.run.suite
        version = (SuiteVersion.objects
                   .filter(suite=root, version=root.version)
                   .order_by('-version').first())
        snapshot = version.snapshot if version else build_topology_snapshot(root)
        self.nodes = {}

        def walk(node):
            self.nodes[node['suite_id']] = node
            for child in node.get('children', []):
                walk(child)

        walk(snapshot)

    @staticmethod
    def _suite_proxy(node):
        return SimpleNamespace(aggregate_method=node.get('aggregate_method', 'weighted_sum'))

    def _subscribed_cases(self, event):
        node = self._active_node or self.nodes[self.run.suite_id]
        case_ids = [
            item['id'] for item in node.get('cases', [])
            if ((item.get('params') or {}).get('trigger') or {}).get('event_type') == event.event_type
        ]
        return list(Case.objects.filter(pk__in=case_ids, status='published'))

    def _execute_cases(self, cases):
        if not cases:
            return []
        fail_stop = self.run.plan.exec_mode == 'fail_stop'
        if fail_stop:
            results = []
            for case in cases:
                try:
                    results.append(self.case_executor.execute(case, self.context))
                except CaseExecutionError:
                    self._record_case_node(case, None, 'failed')
                    raise
            return results
        if self.run.plan.exec_mode == 'parallel' and len(cases) > 1 and self.use_threads:
            with ThreadPoolExecutor(max_workers=len(cases)) as pool:
                return list(pool.map(
                    lambda case: self.case_executor.execute(case, self.context), cases
                ))
        return [self.case_executor.execute(case, self.context) for case in cases]

    def _record_case_node(self, case, result, status='completed'):
        node_run = NodeRun.objects.create(
            run=self.run, node_type='case', case=case,
            suite_id=self._active_node['suite_id'] if self._active_node else None,
            status=status,
            direction=result.direction if result else 0,
            result=dict(result.payload) if result else {},
        )
        if status != 'running':
            node_run.ended_at = timezone.now()
            node_run.save(update_fields=['ended_at'])
        return node_run

    def _execute_node(self, node, event_type, payload):
        """递归执行一个子 Suite 节点：执行其 Case、同步路由其出边（子分支
        join 汇合）、聚合出该节点方向，并回发 CASE_COMPLETED 事件。"""
        self._active_node = node
        node_run = NodeRun.objects.create(
            run=self.run, node_type='suite', suite_id=node['suite_id'], status='running',
        )
        synthetic = SimpleNamespace(event_type=event_type, payload=payload)
        cases = self._subscribed_cases(synthetic)
        results = self._execute_cases(cases)
        aggregate_results = []
        for case, result in zip(cases, results):
            self._record_case_node(case, result)
            self.direction = result.direction
            self.context.update(result.payload)
            self.node_snapshots[str(case.pk)] = result.payload
            aggregate_results.append({
                'direction': result.direction,
                'weight': (case.params or {}).get('weight', 1.0),
            })
            if result.order:
                self.orders.append(result.order)
        # 同步递归路由出边（出边按 CASE_COMPLETED 匹配）：子 Suite 分支执行
        # 完毕（join）后并入本节点聚合
        self._route_edges(SimpleNamespace(event_type=EventType.CASE_COMPLETED,
                                          payload=payload), aggregate_results)
        direction = 0
        if aggregate_results:
            direction = aggregate_directions(self._suite_proxy(node), aggregate_results)
            self.direction = direction
        node_run.direction = direction
        node_run.status = 'completed'
        node_run.result = {'direction': direction}
        node_run.ended_at = timezone.now()
        node_run.save(update_fields=['direction', 'status', 'result', 'ended_at'])
        # 回发完成事件，保证事件驱动链路（跨 Suite/异步场景）可继续
        completion = dict(payload or {})
        completion['direction'] = direction
        completion['target_suite_id'] = node['suite_id']
        completion['_routed'] = True
        enqueue_event(self.run, EventType.CASE_COMPLETED,
                      source=f'suite:{node["suite_id"]}', payload=completion)
        return direction

    def _route_edges(self, event, aggregate_results):
        """按快照边路由：子 Suite 递归同步执行（分支汇合即 join），分支结果
        按边权重并入父节点聚合。并行模式下多分支线程并发。"""
        node = self._active_node or self.nodes[self.run.suite_id]
        event_data = {'event_type': event.event_type, **(event.payload or {})}
        matched = [
            edge for edge in node.get('edges', [])
            if event_condition_matches(
                edge.get('event_condition') or edge.get('condition') or {}, event_data)
        ]
        if not matched:
            return

        def run_branch(edge):
            key = (node['suite_id'], edge['to_suite_id'])
            if key in self._fired_edges:
                return None
            self._fired_edges.add(key)
            child = self.nodes.get(edge['to_suite_id'])
            if not child:
                return None
            next_event = (edge.get('event_condition') or {}).get('next_event', EventType.CASE_START)
            direction = self._execute_node(child, next_event, dict(event.payload or {}))
            return {'direction': direction, 'weight': edge.get('weight', 1.0)}

        if len(matched) > 1 and self.run.plan.exec_mode == 'parallel' and self.use_threads:
            with ThreadPoolExecutor(max_workers=len(matched)) as pool:
                branch_results = [item for item in pool.map(run_branch, matched) if item]
        else:
            branch_results = []
            for edge in matched:
                item = run_branch(edge)
                if item:
                    branch_results.append(item)

        # join：所有分支完成后汇合，分支结果并入调用方聚合
        # （方向统一由调用方 aggregate_directions 决定，避免覆盖）
        aggregate_results.extend(branch_results)

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
        if self.context_builder is not None and not self.context.get('market_data'):
            from .fixture import DataContextBuilder
            builder = self.context_builder if isinstance(
                self.context_builder, DataContextBuilder
            ) else DataContextBuilder(broker=self.context_builder)
            self.context = builder.build(self.run.symbol, context=self.context)
        self._load_topology()
        try:
            while self.run.event_queue:
                event = Event.objects.get(pk=self.run.event_queue[0], run=self.run)
                event.status = 'processing'
                event.save(update_fields=['status'])
                payload = event.payload or {}
                if not payload.get('_routed'):
                    target_suite_id = payload.get('target_suite_id') or self.run.suite_id
                    self._active_node = self.nodes.get(target_suite_id) or self.nodes[self.run.suite_id]
                    if target_suite_id != self.run.suite_id:
                        self.run.suite = Suite.objects.get(pk=target_suite_id)
                        self.run.save(update_fields=['suite'])
                        self.run.refresh_from_db()
                    self._execute_node(self._active_node, event.event_type, payload)
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
    def __init__(self, case_executor=None, broker=None, risk_controller=None,
                 data_context_builder=None, use_threads=True):
        self.case_executor = case_executor
        self.broker = broker
        self.risk_controller = risk_controller
        self.data_context_builder = data_context_builder
        self.use_threads = use_threads

    def run(self, plan, symbol, payload=None):
        run = create_suite_run(plan, symbol, payload)
        return EventLoop(
            run, self.case_executor, payload, self.broker, self.risk_controller,
            context_builder=self.data_context_builder, use_threads=self.use_threads,
        ).run_to_completion()

    async def arun(self, plan, symbol, payload=None):
        return await sync_to_async(self.run, thread_sensitive=True)(plan, symbol, payload)