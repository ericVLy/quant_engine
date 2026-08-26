from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.execution.events import EventType
from apps.execution.models import ExecutionLog, SuiteRun
from apps.watchlists.models import Group, Symbol

from .models import Plan
from apps.suites.models import Suite


class PlanAPITest(APITestCase):
	def setUp(self):
		self.url = '/api/plans/'
		self.suite = Suite.objects.create(name='策略 Suite')
		self.published_suite = Suite.objects.create(name='已发布 Suite', status='published')

	def plan_data(self, **overrides):
		data = {
			'name': '手动计划',
			'root_suite': self.suite.id,
			'trigger_type': 'manual',
			'symbol_scope': {'type': 'symbols', 'symbol_codes': ['000001']},
		}
		data.update(overrides)
		return data

	def create_plan(self, **overrides):
		data = self.plan_data(**overrides)
		data['root_suite'] = self.suite
		return Plan.objects.create(**data)

	def test_create_update_and_filter_plan(self):
		response = self.client.post(self.url, self.plan_data(), format='json')
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		plan_id = response.data['id']

		response = self.client.patch(
			f'{self.url}{plan_id}/', {'name': '更新计划'}, format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		response = self.client.get(self.url, {'trigger_type': 'manual', 'search': '更新'})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(len(response.data), 1)

	def test_validate_time_trigger_cron(self):
		response = self.client.post(
			self.url,
			self.plan_data(trigger_type='time', cron_expr='0 9 * * 1-5'),
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

		response = self.client.post(
			self.url,
			self.plan_data(trigger_type='time', cron_expr='daily'),
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('cron_expr', response.data)

	def test_validate_event_trigger(self):
		response = self.client.post(
			self.url,
			self.plan_data(trigger_type='event', event_type=EventType.PRICE_SURGE),
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)

		response = self.client.post(
			self.url,
			self.plan_data(trigger_type='event', event_type='UNKNOWN_EVENT'),
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('event_type', response.data)

	def test_validate_symbol_scope(self):
		response = self.client.post(
			self.url,
			self.plan_data(symbol_scope={'type': 'invalid'}),
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('symbol_scope', response.data)

	def test_publish_requires_published_root_suite(self):
		plan = self.create_plan()
		response = self.client.post(f'{self.url}{plan.id}/publish/')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

		plan.root_suite = self.published_suite
		plan.save(update_fields=['root_suite'])
		response = self.client.post(f'{self.url}{plan.id}/publish/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		plan.refresh_from_db()
		self.assertEqual(plan.status, 'published')
		self.assertEqual(plan.version, 2)

	def test_resolve_symbols_endpoint(self):
		symbol = Symbol.objects.create(code='000001', name='平安银行', market='A')
		plan = self.create_plan()
		response = self.client.get(f'{self.url}{plan.id}/symbols/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data[0]['id'], symbol.id)

	def test_resolve_group_symbols_endpoint(self):
		symbol = Symbol.objects.create(code='000002', name='万科A', market='A')
		group = Group.objects.create(name='蓝筹')
		group.symbols.add(symbol)
		plan = self.create_plan(
			symbol_scope={'type': 'groups', 'group_ids': [group.id]}
		)
		response = self.client.get(f'{self.url}{plan.id}/symbols/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data[0]['code'], '000002')

	def test_delete_plan_with_run_returns_conflict(self):
		plan = self.create_plan()
		SuiteRun.objects.create(plan=plan, suite=self.suite, symbol='000001')
		response = self.client.delete(f'{self.url}{plan.id}/')
		self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

	def test_delete_plan_with_log_returns_conflict(self):
		plan = self.create_plan()
		ExecutionLog.objects.create(
			plan=plan, symbol='000001', final_direction=0, status='success'
		)
		response = self.client.delete(f'{self.url}{plan.id}/')
		self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

	def test_delete_unreferenced_plan(self):
		plan = self.create_plan()
		response = self.client.delete(f'{self.url}{plan.id}/')
		self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
		self.assertFalse(Plan.objects.filter(pk=plan.id).exists())
