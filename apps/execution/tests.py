import json
import logging
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from unittest.mock import patch, MagicMock

from apps.plans.models import Plan
from apps.suites.models import Suite

from .models import SuiteRun, Event, EventTypeRegistry, ExecutionLog, Order
from .events import EventType
from .registry import EventRegistry
from .services import (
    ExecutionError,
    complete_suite_run,
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
        data = response.data
        event_names = [item['name'] for item in data]
        self.assertIn(EventType.SUITE_INIT, event_names)
        self.assertIn(EventType.CASE_COMPLETED, event_names)

    def test_create_custom_event_type(self):
        data = {
            'name': 'MY_CUSTOM_EVENT',
            'scope': 'user',
            'description': '用户自定义事件',
        }
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        EventRegistry.clear_cache()
        obj = EventTypeRegistry.objects.get(name='MY_CUSTOM_EVENT')
        self.assertEqual(obj.scope, 'user')

        self.assertTrue(EventRegistry.validate('MY_CUSTOM_EVENT'))

    def test_duplicate_builtin_event_type(self):
        data = {
            'name': EventType.SUITE_INIT,
            'scope': 'user',
        }
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('系统内置事件', response.data['name'][0])


class EventRegistryTest(TestLoggingMixin, TestCase):
    """测试事件注册中心功能"""

    def test_validate_builtin(self):
        self.assertTrue(EventRegistry.validate(EventType.SUITE_INIT))
        self.assertTrue(EventRegistry.validate(EventType.CASE_COMPLETED))
        self.assertFalse(EventRegistry.validate('NON_EXISTENT_EVENT'))

    def test_register_custom(self):
        EventRegistry.register('TEST_EVENT', scope='user', description='测试事件')
        EventRegistry.clear_cache()
        EventRegistry._get_cache()
        self.assertTrue(EventRegistry.validate('TEST_EVENT'))
        info = EventRegistry.get('TEST_EVENT')
        self.assertEqual(info['scope'], 'user')
        self.assertEqual(info['description'], '测试事件')
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
        EventRegistry.register('TEST_EVENT_TYPE', scope='user')
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