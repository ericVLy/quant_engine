import json
import logging
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from unittest.mock import patch, MagicMock

from apps.plans.models import Plan
from apps.suites.models import Edge, Suite

from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order
from .events import EventType
from .registry import EventRegistry
from .services import (
    ExecutionError,
    complete_suite_run,
    enqueue_event,
    process_next_event,
    start_suite_run,
    trigger_plan,
)

logger = logging.getLogger(__name__)

User = get_user_model()


class TestLoggingMixin:
    def tearDown(self):
        try:
            super().tearDown()
        finally:
            outcome = getattr(self, '_outcome', None)
            result = getattr(outcome, 'result', None)
            test_name = self.id()
            failures = []

            if result is not None:
                failures.extend(result.failures)
                failures.extend(result.errors)

            test_failures = [failure for failure in failures if failure[0] is self]
            if test_failures:
                exception_details = '\n'.join(failure[1] for failure in test_failures)
                logger.error('测试失败: %s\n异常:\n%s', test_name, exception_details)
            else:
                logger.info('测试成功: %s', test_name)


class EventTypeRegistryTest(TestLoggingMixin, APITestCase):
    """测试事件类型注册表 API"""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_superuser(username='admin', password='admin123')
        self.client.force_authenticate(user=self.admin)
        self.list_url = '/api/execution/event-types/'

    def test_list_event_types(self):
        response = self.client.get(self.list_url + 'list-all/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data['results']
        event_names = [item['name'] for item in data]
        self.assertIn(EventType.SUITE_INIT, event_names)
        self.assertIn(EventType.CASE_COMPLETED, event_names)

    def test_create_custom_event_type(self):
        data = {
            'name': 'MY_CUSTOM_EVENT',
            'scope': 'user',
            'description': '用户自定义事件',
            'base_event_type': EventType.CASE_COMPLETED,
        }
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        EventRegistry.clear_cache()
        obj = EventTypeRegistry.objects.get(name='MY_CUSTOM_EVENT')
        self.assertEqual(obj.scope, 'user')
        self.assertEqual(obj.base_event_type, EventType.CASE_COMPLETED)

        self.assertTrue(EventRegistry.validate('MY_CUSTOM_EVENT'))
        self.assertEqual(
            EventRegistry.get_base_event_type('MY_CUSTOM_EVENT'),
            EventType.CASE_COMPLETED,
        )

    def test_duplicate_builtin_event_type(self):
        data = {
            'name': EventType.SUITE_INIT,
            'scope': 'user',
        }
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('系统内置事件', response.data['name'][0])


class UserEventOverlayConstraintTest(TestLoggingMixin, APITestCase):
    """叠加约束：用户自定义事件仅支持叠加在系统自带事件之上"""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_superuser(username='overlay-admin', password='admin123')
        self.client.force_authenticate(user=self.admin)
        self.list_url = '/api/execution/event-types/'

    def _payload(self, **overrides):
        data = {
            'name': 'OVERLAY_SURGE',
            'scope': 'user',
            'description': '叠加在系统事件上的用户事件',
            'base_event_type': EventType.PRICE_SURGE,
        }
        data.update(overrides)
        return data

    def test_user_event_with_builtin_base_is_accepted(self):
        response = self.client.post(self.list_url, self._payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        EventRegistry.clear_cache()
        obj = EventTypeRegistry.objects.get(name='OVERLAY_SURGE')
        self.assertEqual(obj.base_event_type, EventType.PRICE_SURGE)
        self.assertTrue(EventRegistry.validate('OVERLAY_SURGE'))
        self.assertEqual(
            EventRegistry.get_base_event_type('OVERLAY_SURGE'), EventType.PRICE_SURGE)

    def test_user_event_requires_base_event(self):
        response = self.client.post(self.list_url, self._payload(base_event_type=''))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        messages = ' '.join(str(value) for value in response.data.values())
        self.assertIn('叠加', messages)
        self.assertFalse(EventRegistry.validate('OVERLAY_SURGE'))

    def test_user_event_base_must_be_builtin(self):
        """不允许以其他用户/插件注册的事件为基（防止叠加套娃）。"""
        EventRegistry.register('OTHER_USER_EVENT', scope='user', base_event_type=EventType.TIMER)
        try:
            response = self.client.post(
                self.list_url,
                self._payload(name='NESTED_OVERLAY', base_event_type='OTHER_USER_EVENT'),
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            messages = ' '.join(str(value) for value in response.data.values())
            self.assertIn('系统自带事件', messages)
            self.assertFalse(EventRegistry.validate('NESTED_OVERLAY'))
        finally:
            EventTypeRegistry.objects.filter(name='OTHER_USER_EVENT').delete()
            EventRegistry.clear_cache()

    def test_system_scope_registration_rejected(self):
        """系统内置事件由代码定义，禁止通过注册表 API 冒充创建。"""
        response = self.client.post(
            self.list_url, self._payload(name='FAKE_SYSTEM_EVENT', scope='system'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(EventRegistry.validate('FAKE_SYSTEM_EVENT'))

    def test_plugin_event_base_optional_but_must_be_builtin(self):
        response = self.client.post(
            self.list_url, self._payload(name='PLUGIN_EVENT', scope='plugin', base_event_type=''))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.post(
            self.list_url, self._payload(name='PLUGIN_OVERLAY', scope='plugin'))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        obj = EventTypeRegistry.objects.get(name='PLUGIN_OVERLAY')
        self.assertEqual(obj.base_event_type, EventType.PRICE_SURGE)

        response = self.client.post(
            self.list_url,
            self._payload(name='PLUGIN_BAD_BASE', scope='plugin', base_event_type='PLUGIN_EVENT'),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_all_exposes_base_event_type(self):
        self.client.post(self.list_url, self._payload())
        response = self.client.get(self.list_url + 'list-all/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_name = {item['name']: item for item in response.data['results']}
        self.assertIsNone(by_name[EventType.SUITE_INIT]['base_event_type'])
        self.assertEqual(by_name['OVERLAY_SURGE']['base_event_type'], EventType.PRICE_SURGE)

    def tearDown(self):
        EventTypeRegistry.objects.filter(
            name__in=['OVERLAY_SURGE', 'PLUGIN_EVENT', 'PLUGIN_OVERLAY']).delete()
        EventRegistry.clear_cache()
        super().tearDown()


class EventOverlayRuntimeTest(TestLoggingMixin, TestCase):
    """叠加事件运行时语义：入队注入基事件 + Edge 条件按基事件回落匹配"""

    def setUp(self):
        self.suite = Suite.objects.create(name='叠加运行时 Suite', status='published')
        self.downstream = Suite.objects.create(
            name='下游 Suite', status='published', parent=self.suite)
        self.plan = Plan.objects.create(
            name='叠加运行时 Plan', root_suite=self.suite, status='published',
            symbol_scope={'type': 'symbols'},
        )
        self.run = SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001', status='running',
            event_queue=[],
        )
        EventRegistry.register('OVERLAY_SURGE', scope='user', base_event_type=EventType.PRICE_SURGE)
        EventRegistry.clear_cache()

    def tearDown(self):
        EventTypeRegistry.objects.filter(name='OVERLAY_SURGE').delete()
        EventRegistry.clear_cache()
        super().tearDown()

    def test_enqueue_overlay_event_injects_base_event_type(self):
        event = enqueue_event(self.run, 'OVERLAY_SURGE', source='test', payload={'symbol': '000001'})
        self.assertEqual(event.payload['base_event_type'], EventType.PRICE_SURGE)
        self.assertEqual(
            Event.objects.get(pk=event.pk).payload['base_event_type'], EventType.PRICE_SURGE)

    def test_builtin_event_payload_has_no_base_event_type(self):
        event = enqueue_event(self.run, EventType.SUITE_INIT, source='test')
        self.assertNotIn('base_event_type', event.payload)

    def test_edge_condition_on_base_event_matches_overlay_event(self):
        """基事件（PRICE_SURGE）的 Edge 条件可被叠加事件命中。"""
        Edge.objects.create(
            from_suite=self.suite, to_suite=self.downstream,
            event_condition={'event_type': EventType.PRICE_SURGE, 'next_event': EventType.CASE_START},
        )
        enqueue_event(self.run, 'OVERLAY_SURGE', source='test')
        processed = process_next_event(self.run)
        self.assertEqual(processed.event_type, 'OVERLAY_SURGE')
        self.assertEqual(processed.status, 'done')

        run = SuiteRun.objects.get(pk=self.run.pk)
        follow_event = Event.objects.get(pk=run.event_queue[-1])
        self.assertEqual(follow_event.event_type, EventType.CASE_START)

    def test_edge_condition_on_unrelated_event_does_not_match(self):
        Edge.objects.create(
            from_suite=self.suite, to_suite=self.downstream,
            event_condition={'event_type': EventType.MACRO_CPI, 'next_event': EventType.CASE_START},
        )
        enqueue_event(self.run, 'OVERLAY_SURGE', source='test')
        process_next_event(self.run)

        run = SuiteRun.objects.get(pk=self.run.pk)
        # 无条件命中的 Edge → 无后续事件入队（已处理的叠加事件本身被弹出）
        self.assertEqual(len(run.event_queue), 0)
        self.assertFalse(
            Event.objects.filter(run=self.run, event_type=EventType.CASE_START).exists())


class EventRegistryTest(TestLoggingMixin, TestCase):
    """测试事件注册中心功能"""

    def test_validate_builtin(self):
        self.assertTrue(EventRegistry.validate(EventType.SUITE_INIT))
        self.assertTrue(EventRegistry.validate(EventType.CASE_COMPLETED))
        self.assertFalse(EventRegistry.validate('NON_EXISTENT_EVENT'))

    def test_register_custom(self):
        EventRegistry.register(
            'TEST_EVENT', scope='user', description='测试事件',
            base_event_type=EventType.SUITE_INIT,
        )
        EventRegistry.clear_cache()
        EventRegistry._get_cache()
        self.assertTrue(EventRegistry.validate('TEST_EVENT'))
        info = EventRegistry.get('TEST_EVENT')
        self.assertEqual(info['scope'], 'user')
        self.assertEqual(info['description'], '测试事件')
        self.assertEqual(info['base_event_type'], EventType.SUITE_INIT)
        EventTypeRegistry.objects.filter(name='TEST_EVENT').delete()
        EventRegistry.clear_cache()


class EventObjectPatternTest(TestLoggingMixin, TestCase):
    """测试事件类型类 + 事件对象实例模式"""

    def test_event_instance_is_built_from_class_definition(self):
        from .events import SuiteInitEvent

        event = SuiteInitEvent(
            source='plan',
            payload={'symbol': '000001'},
            metadata={'trigger': 'manual'},
        )

        self.assertEqual(event.event_type, EventType.SUITE_INIT)
        self.assertEqual(event.source, 'plan')
        self.assertEqual(event.payload['symbol'], '000001')
        self.assertEqual(event.metadata['trigger'], 'manual')

    def test_event_specific_attributes_and_helpers(self):
        from .events import PriceSurgeEvent, TimerEvent

        price_event = PriceSurgeEvent(
            symbol='000001',
            market='A',
            price=12.5,
            change_pct=2.1,
            volume=120000,
            source='market',
        )

        self.assertEqual(price_event.symbol, '000001')
        self.assertEqual(price_event.price, 12.5)
        self.assertTrue(price_event.is_upward())
        self.assertIn('000001', price_event.summary())

        timer_event = TimerEvent(
            trigger_time='2026-09-02 12:00:00',
            interval_seconds=60,
            source='scheduler',
        )

        self.assertEqual(timer_event.interval_seconds, 60)
        self.assertEqual(timer_event.trigger_time, '2026-09-02 12:00:00')


class SuiteRunAPITest(TestLoggingMixin, APITestCase):
    """测试 SuiteRun API"""

    def setUp(self):
        self.client = APIClient()
        self.suite = Suite.objects.create(name='测试 Suite')
        self.plan = Plan.objects.create(
            name='测试 Plan',
            root_suite=self.suite,
            status='published',
            symbol_scope={'type': 'symbols'},
        )

    def test_suite_run_model(self):
        run = SuiteRun.objects.create(
            plan=self.plan,
            suite=self.suite,
            symbol='000001',
            status='pending',
            event_queue=[],
        )
        self.assertEqual(run.status, 'pending')
        self.assertEqual(run.symbol, '000001')
        self.assertEqual(run.event_queue, [])
        run.delete()

    def test_lifecycle_and_event_queue(self):
        run = trigger_plan(self.plan.id, ['000001'])[0]
        self.assertEqual(run.status, 'pending')
        self.assertEqual(run.event_queue, [run.events.get().id])

        start_suite_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        self.assertIsNotNone(run.started_at)

        first_event = process_next_event(run)
        self.assertEqual(first_event.event_type, EventType.SUITE_INIT)
        run.refresh_from_db()
        self.assertEqual(run.event_queue, [run.events.get(event_type=EventType.SUITE_START).id])
        process_next_event(run)
        run.refresh_from_db()
        self.assertEqual(run.event_queue, [])

        complete_suite_run(run)
        self.assertEqual(run.status, 'completed')
        self.assertIsNotNone(run.ended_at)

    def test_cannot_complete_with_pending_events(self):
        run = trigger_plan(self.plan.id, ['000001'])[0]
        with self.assertRaises(ExecutionError):
            complete_suite_run(run)


class EventAPITest(TestLoggingMixin, APITestCase):
    """测试 Event API"""

    def setUp(self):
        self.client = APIClient()
        self.suite = Suite.objects.create(name='事件测试 Suite')
        self.plan = Plan.objects.create(
            name='事件测试 Plan',
            root_suite=self.suite,
            status='published',
        )
        self.run = SuiteRun.objects.create(
            plan=self.plan,
            suite=self.suite,
            symbol='000001',
            status='running',
            event_queue=[]
        )
        EventRegistry.register('TEST_EVENT_TYPE', scope='user', base_event_type=EventType.CASE_COMPLETED)
        EventRegistry.clear_cache()
        EventRegistry._get_cache()

    def tearDown(self):
        EventTypeRegistry.objects.filter(name='TEST_EVENT_TYPE').delete()
        EventRegistry.clear_cache()
        super().tearDown()

    def test_create_event(self):
        from .serializers import EventSerializer
        data = {
            'run': self.run.id,
            'event_type': 'TEST_EVENT_TYPE',
            'source': 'test',
            'payload': {'key': 'value'},
            'status': 'pending',
        }
        serializer = EventSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        event = serializer.save()
        self.assertEqual(event.event_type, 'TEST_EVENT_TYPE')
        self.assertEqual(event.source, 'test')
        self.assertEqual(event.payload, {'key': 'value'})

    def test_invalid_event_type(self):
        from .serializers import EventSerializer
        data = {
            'run': self.run.id,
            'event_type': 'INVALID_EVENT',
            'source': 'test',
            'payload': {},
        }
        serializer = EventSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn('event_type', serializer.errors)

    def test_trigger_plan_api(self):
        response = self.client.post(
            '/api/execution/trigger/',
            {'plan_id': self.plan.id, 'symbols': ['000001', '000002']},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response_data = json.loads(response.content)
        self.assertEqual(len(response_data['run_ids']), 2)
        self.assertEqual(SuiteRun.objects.filter(plan=self.plan).count(), 3)


class ExecutionLogAPITest(TestLoggingMixin, APITestCase):
    """测试 ExecutionLog API"""

    def test_create_log(self):
        log = ExecutionLog.objects.create(
            symbol='000001',
            final_direction=1,
            status='success',
            node_snapshots={'test': 'data'}
        )
        self.assertEqual(log.symbol, '000001')
        self.assertEqual(log.final_direction, 1)
        self.assertEqual(log.status, 'success')
        log.delete()


class PaginationContractTest(TestLoggingMixin, APITestCase):
    """N-01：execution 模块列表接口统一分页契约测试。

    验证所有列表接口返回 `{count, next, previous, page, total_pages, results}`
    结构，且 `results` 为列表数据（旧客户端可继续通过字段兼容读取）。
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='pager', password='pager-pass-123',
            is_staff=True,
        )
        self.client.force_authenticate(user=self.user)
        self.suite = Suite.objects.create(name='分页 Suite')
        self.plan = Plan.objects.create(
            name='分页 Plan', root_suite=self.suite, status='published',
            symbol_scope={'type': 'symbols'},
        )

    def test_execution_list_endpoints_are_paginated(self):
        from .models import Event, ExecutionLog, Order

        run = SuiteRun.objects.create(
            plan=self.plan, suite=self.suite, symbol='000001', status='running',
            event_queue=[],
        )
        Event.objects.create(run=run, event_type=EventType.SUITE_INIT)
        log = ExecutionLog.objects.create(
            plan=self.plan, symbol='000001', final_direction=1, status='success',
        )
        Order.objects.create(
            log=log, symbol='000001', direction='buy', price=10.5, volume=100,
            status='pending',
        )
        from .models import AlertChannel
        AlertChannel.objects.create(channel_type='in_app', is_enabled=True)

        endpoints = [
            '/api/execution/runs/',
            '/api/execution/events/',
            '/api/execution/logs/',
            '/api/execution/orders/',
            '/api/execution/alert-channels/',
        ]
        for url in endpoints:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(set(response.data.keys()),
                                 {'count', 'next', 'previous', 'page', 'total_pages', 'results'})
                self.assertEqual(response.data['count'], 1)
                self.assertIsInstance(response.data['results'], list)

    def test_event_types_list_all_paginated(self):
        EventRegistry.register('PAGING_EVENT', scope='user', base_event_type=EventType.SUITE_INIT)
        EventRegistry.clear_cache()
        EventRegistry._get_cache()
        try:
            response = self.client.get('/api/execution/event-types/list-all/')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(set(response.data.keys()),
                             {'count', 'next', 'previous', 'page', 'total_pages', 'results'})
            names = [item['name'] for item in response.data['results']]
            self.assertIn(EventType.SUITE_INIT, names)
            self.assertIn('PAGING_EVENT', names)
        finally:
            EventTypeRegistry.objects.filter(name='PAGING_EVENT').delete()
            EventRegistry.clear_cache()

    def test_runs_page_size_and_limit_alias(self):
        for status_ in ('pending', 'running'):
            for idx in range(12):
                SuiteRun.objects.create(
                    plan=self.plan, suite=self.suite, symbol=f'000{idx:03d}',
                    status=status_, event_queue=[],
                )
        response = self.client.get('/api/execution/runs/', {'page_size': 8})
        self.assertEqual(response.data['count'], 24)
        self.assertEqual(response.data['total_pages'], 3)
        self.assertEqual(len(response.data['results']), 8)
        # 旧客户端兼容：limit 可作为 page_size 别名
        limit_response = self.client.get('/api/execution/runs/', {'limit': 100})
        self.assertEqual(len(limit_response.data['results']), 24)