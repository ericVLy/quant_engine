from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cases.models import Case
from apps.execution.events import EventType
from apps.execution.models import ExecutionLog, SuiteRun
from apps.watchlists.models import Group, Symbol

from .models import Plan
from .models import PlanVersion
from .serializers import PlanSerializer
from .services import rollback_plan
from apps.suites.models import Suite


class PlanAPITest(APITestCase):
	def setUp(self):
		self.url = '/api/plans/'
		self.suite = Suite.objects.create(name='策略 Suite')
		self.published_suite = Suite.objects.create(name='已发布 Suite', status='published')
		# 标的范围由 Case 声明：给已发布 Suite 挂一个声明 000001 的已发布 Case
		Symbol.objects.get_or_create(code='000001', defaults={'name': '平安银行', 'market': 'A'})
		self.signal_case = Case.objects.create(
			name='信号', node_type='signal', status='published',
			params={
				'trigger': {'event_type': 'SUITE_INIT'},
				'symbol_scope': {'type': 'symbols', 'symbol_codes': ['000001']},
			},
		)
		self.published_suite.cases.set([self.signal_case])

	def plan_data(self, **overrides):
		data = {
			'name': '手动计划',
			'root_suite': self.suite.id,
			'trigger_type': 'manual',
		}
		data.update(overrides)
		return data

	def create_plan(self, **overrides):
		data = self.plan_data(**overrides)
		data['root_suite'] = overrides.get('root_suite', self.suite)
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
		self.assertEqual(response.data['count'], 1)

	def test_plan_list_paginated(self):
		for index in range(25):
			self.create_plan(name=f'批量计划 {index}')
		response = self.client.get(self.url, {'page_size': 10})
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['count'], 25)
		self.assertEqual(response.data['total_pages'], 3)
		self.assertEqual(len(response.data['results']), 10)

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

	def test_plan_ignores_symbol_scope_field(self):
		"""标的范围已下沉到 Case：Plan 模型不再持有 symbol_scope 字段。

		DRF 对未知输入键是静默忽略（不报错），因此这里断言的是
		"该字段不会落到 Plan 上"，标的以 Case 声明为准。
		"""
		response = self.client.post(
			self.url,
			self.plan_data(symbol_scope={'type': 'symbols', 'symbol_codes': ['000001']}),
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertNotIn('symbol_scope', response.data)
		self.assertFalse(hasattr(Plan.objects.get(pk=response.data['id']), 'symbol_scope'))

	def test_publish_requires_a_case_declaring_symbols(self):
		"""树内没有任何已发布 Case 声明标的时，Plan 不得发布。"""
		empty_suite = Suite.objects.create(name='空 Suite', status='published')
		plan = self.create_plan(root_suite=empty_suite)
		response = self.client.post(f'{self.url}{plan.id}/publish/')
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('symbol_scope', str(response.data))

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
		# 标的集合 = 编排树内 Case 声明的并集（000001 由 setUp 的 Case 声明）
		plan = self.create_plan(root_suite=self.published_suite)
		response = self.client.get(f'{self.url}{plan.id}/symbols/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['count'], 1)
		self.assertEqual(response.data['results'][0]['code'], '000001')

	def test_resolve_group_symbols_endpoint(self):
		symbol = Symbol.objects.create(code='000002', name='万科A', market='A')
		group = Group.objects.create(name='蓝筹')
		group.symbols.add(symbol)
		suite = Suite.objects.create(name='分组 Suite', status='published')
		suite.cases.set([Case.objects.create(
			name='分组信号', node_type='signal', status='published',
			params={
				'trigger': {'event_type': 'SUITE_INIT'},
				'symbol_scope': {'type': 'groups', 'group_ids': [group.id]},
			},
		)])
		plan = self.create_plan(root_suite=suite)
		response = self.client.get(f'{self.url}{plan.id}/symbols/')
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['count'], 1)
		self.assertEqual(response.data['results'][0]['code'], '000002')

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

	def test_validate_retry_policy(self):
		response = self.client.post(
			self.url,
			self.plan_data(retry_policy={'max_retries': -1}),
			format='json',
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
		self.assertIn('retry_policy', response.data)

	def test_rollback_plan_creates_new_published_version(self):
		plan = self.create_plan(root_suite=self.published_suite)
		plan.status = 'published'
		plan.version = 2
		plan.name = '当前版本'
		plan.save(update_fields=['status', 'version', 'name', 'updated_at'])
		PlanVersion.objects.create(
			plan=plan, version=1,
			snapshot={
				'name': '历史版本', 'root_suite_id': self.published_suite.id,
				'trigger_type': 'manual',
				'exec_mode': 'serial', 'retry_policy': {}, 'status': 'published',
			},
		)

		response = self.client.post(
			f'{self.url}{plan.id}/rollback/', {'version': 1}, format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		plan.refresh_from_db()
		self.assertEqual(plan.name, '历史版本')
		self.assertEqual(plan.version, 3)
		self.assertTrue(PlanVersion.objects.filter(plan=plan, version=3).exists())

	def test_rollback_rejects_unknown_version(self):
		plan = self.create_plan()
		response = self.client.post(
			f'{self.url}{plan.id}/rollback/', {'version': 99}, format='json'
		)
		self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
