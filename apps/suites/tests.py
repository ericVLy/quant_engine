from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cases.models import Case
from apps.plans.models import Plan

from .models import Edge, Suite
from .services import SuiteError, publish_suite, validate_dag


class SuiteAPITest(APITestCase):
	def setUp(self):
		self.url = '/api/suites/'
		self.suite = Suite.objects.create(name='主策略')
		self.case = Case.objects.create(
			name='已发布信号', node_type='signal', status='published'
		)

	def test_suite_crud_and_filter(self):
		Suite.objects.create(name='过滤策略', aggregate_method='vote')
		response = self.client.get(self.url, {'search': '主', 'status': 'draft'})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data), 1)
		self.assertEqual(response.data[0]['name'], '主策略')

		response = self.client.patch(
			f'{self.url}{self.suite.id}/', {'name': '更新策略'}, format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.suite.refresh_from_db()
		self.assertEqual(self.suite.name, '更新策略')

	def test_topology_update_and_read(self):
		target = Suite.objects.create(name='下游策略')
		response = self.client.post(
			f'{self.url}{self.suite.id}/topology/',
			{
				'case_ids': [self.case.id],
				'edges': [{
					'from_suite': self.suite.id,
					'to_suite': target.id,
					'event_condition': {'event_type': 'CASE_COMPLETED'},
					'weight': 0.5,
				}],
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data['cases']), 1)
		self.assertEqual(len(response.data['edges']), 1)
		self.assertEqual(response.data['edges'][0]['weight'], 0.5)

		response = self.client.get(f'{self.url}{self.suite.id}/topology/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)

	def test_topology_rejects_unknown_event_condition_fields(self):
		target = Suite.objects.create(name='下游策略')
		response = self.client.post(
			f'{self.url}{self.suite.id}/topology/',
			{
				'case_ids': [self.case.id],
				'edges': [{
					'from_suite': self.suite.id,
					'to_suite': target.id,
					'event_condition': {'event_type': 'CASE_COMPLETED', 'freeform': 'bad'},
					'weight': 0.5,
				}],
			},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

	def test_topology_rejects_cycle(self):
		target = Suite.objects.create(name='下游策略')
		Edge.objects.create(from_suite=self.suite, to_suite=target)
		response = self.client.post(
			f'{self.url}{target.id}/topology/',
			{'edges': [{'from_suite': target.id, 'to_suite': self.suite.id}]},
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertTrue(Edge.objects.filter(from_suite=self.suite, to_suite=target).exists())

	def test_publish_requires_published_cases(self):
		draft_case = Case.objects.create(name='草稿信号', node_type='signal')
		self.suite.cases.add(draft_case)
		response = self.client.post(f'{self.url}{self.suite.id}/publish/')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.suite.refresh_from_db()
		self.assertEqual(self.suite.status, 'draft')

	def test_publish_increments_version(self):
		self.suite.cases.add(self.case)
		response = self.client.post(f'{self.url}{self.suite.id}/publish/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.suite.refresh_from_db()
		self.assertEqual(self.suite.status, 'published')
		self.assertEqual(self.suite.version, 2)

	def test_delete_referenced_suite_returns_conflict(self):
		Plan.objects.create(name='引用计划', root_suite=self.suite)
		response = self.client.delete(f'{self.url}{self.suite.id}/')
		self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
		self.assertTrue(Suite.objects.filter(pk=self.suite.pk).exists())

	def test_delete_unreferenced_suite(self):
		response = self.client.delete(f'{self.url}{self.suite.id}/')
		self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
		self.assertFalse(Suite.objects.filter(pk=self.suite.pk).exists())


class SuiteServiceTest(TestCase):
	def test_validate_dag_rejects_cycle(self):
		first = Suite.objects.create(name='一')
		second = Suite.objects.create(name='二')
		Edge.objects.create(from_suite=first, to_suite=second)
		Edge.objects.create(from_suite=second, to_suite=first)
		with self.assertRaises(SuiteError):
			validate_dag(first)

	def test_publish_suite_requires_published_case(self):
		suite = Suite.objects.create(name='未完成')
		case = Case.objects.create(name='草稿', node_type='filter')
		suite.cases.add(case)
		with self.assertRaises(SuiteError):
			publish_suite(suite)
