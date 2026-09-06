from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.exceptions import ValidationError

from apps.execution.events import EventType
from apps.execution.registry import EventRegistry
from apps.suites.models import Suite

from .models import Case, CaseVersion
from .serializers import validate_case_schema


class CaseAPITest(APITestCase):
    def setUp(self):
        self.url = '/api/cases/'

    def test_create_case_with_valid_trigger(self):
        response = self.client.post(self.url, {
            'name': 'RSI 信号',
            'node_type': 'signal',
            'params': {'trigger': {'event_type': EventType.SUITE_INIT}, 'period': 14},
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        case = Case.objects.get()
        self.assertEqual(case.params['trigger']['event_type'], EventType.SUITE_INIT)
        self.assertEqual(case.status, 'draft')
        self.assertEqual(case.version, 1)

    def test_reject_case_with_unknown_trigger(self):
        response = self.client.post(self.url, {
            'name': '非法触发器',
            'node_type': 'filter',
            'params': {'trigger': {'event_type': 'UNKNOWN_EVENT'}},
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('params', response.data)

    def test_params_must_be_object(self):
        response = self.client.post(self.url, {
            'name': '参数错误',
            'node_type': 'signal',
            'params': ['not', 'an', 'object'],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('params', response.data)

    def test_reject_case_params_with_unknown_fields(self):
        response = self.client.post(self.url, {
            'name': '非法参数字段',
            'node_type': 'signal',
            'params': {
                'trigger': {'event_type': EventType.SUITE_INIT},
                'period': 14,
                'random_extra': 'not-allowed',
            },
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('params', response.data)

    def test_reject_nested_result_with_invalid_order(self):
        response = self.client.post(self.url, {
            'name': '非法订单',
            'node_type': 'executor',
            'params': {
                'trigger': {'event_type': EventType.SUITE_INIT},
                'result': {
                    'direction': 1,
                    'payload': {'score': 1},
                    'order': {'direction': 'buy', 'price': 0, 'volume': 0},
                },
            },
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('params', response.data)

    def test_reject_filter_field_without_threshold(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('filter', {
                'filter': {'op': 'keep', 'field': 'rsi'},
            })

    def test_reject_unknown_filter_operator(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('filter', {
                'filter': {'op': 'greater_than', 'threshold': 70},
            })

    def test_reject_invalid_verdict_component(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('verdict', {
                'verdict': {'method': 'vote', 'components': [
                    {'indicator': 'rsi', 'period': 0},
                ]},
            })

    def test_accept_valid_deep_verdict_configuration(self):
        validate_case_schema('verdict', {
            'verdict': {
                'method': 'weighted_sum',
                'components': [
                    {'indicator': 'rsi', 'period': 14, 'weight': 1.0},
                    {'calculation': 'mean', 'period': 5, 'weight': 0.5},
                ],
            },
        })

    def test_reject_boolean_as_numeric_parameter(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('signal', {'period': True})

    def test_reject_unknown_indicator(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('signal', {'indicator': 'unknown_indicator'})

    def test_reject_invalid_macd_period_order(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('signal', {
                'indicator': 'macd', 'fast': 26, 'slow': 12, 'signal': 9,
            })

    def test_reject_inverted_rsi_thresholds(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('signal', {
                'indicator': 'rsi', 'threshold_oversold': 80,
                'threshold_overbought': 20,
            })

    def test_reject_zero_verdict_weights(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('verdict', {
                'verdict': {'method': 'weighted_sum', 'components': [
                    {'indicator': 'rsi', 'period': 14, 'weight': 0},
                ]},
            })

    def test_reject_duplicate_verdict_indicators(self):
        with self.assertRaises(ValidationError):
            validate_case_schema('verdict', {
                'verdict': {'method': 'vote', 'components': [
                    {'indicator': 'rsi', 'period': 14},
                    {'indicator': 'rsi', 'period': 7},
                ]},
            })

    def test_list_filter_and_search_cases(self):
        Case.objects.create(name='RSI 信号', node_type='signal')
        Case.objects.create(name='均线过滤', node_type='filter')

        response = self.client.get(self.url, {'node_type': 'signal', 'search': 'RSI'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], 'RSI 信号')

    def test_publish_case_increments_version(self):
        case = Case.objects.create(name='待发布', node_type='executor')

        response = self.client.post(f'{self.url}{case.id}/publish/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        case.refresh_from_db()
        self.assertEqual(case.status, 'published')
        self.assertEqual(case.version, 2)
        self.assertTrue(CaseVersion.objects.filter(case=case, version=2).exists())

    def test_case_versions_endpoint_returns_snapshots(self):
        case = Case.objects.create(name='历史', node_type='signal', status='published', version=2)
        CaseVersion.objects.create(
            case=case, version=2, name=case.name, node_type=case.node_type,
            params={'direction': 1}, status='published',
        )

        response = self.client.get(f'{self.url}{case.id}/versions/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['params']['direction'], 1)

    def test_delete_referenced_case_returns_conflict(self):
        case = Case.objects.create(name='已引用', node_type='signal')
        suite = Suite.objects.create(name='引用 Suite')
        suite.cases.add(case)

        response = self.client.delete(f'{self.url}{case.id}/')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Case.objects.filter(pk=case.pk).exists())

    def test_delete_unreferenced_case(self):
        case = Case.objects.create(name='未引用', node_type='signal')

        response = self.client.delete(f'{self.url}{case.id}/')

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Case.objects.filter(pk=case.pk).exists())


class CaseModelTest(TestCase):
    def test_event_registry_accepts_custom_trigger(self):
        EventRegistry.register('CASE_CUSTOM_TRIGGER', scope='user')
        case = Case.objects.create(
            name='自定义触发',
            node_type='signal',
            params={'trigger': {'event_type': 'CASE_CUSTOM_TRIGGER'}},
        )
        self.assertTrue(EventRegistry.validate(case.params['trigger']['event_type']))
        EventRegistry.clear_cache()
