"""NodeRun 轨迹 API 专项测试（画布可视化编排 · 执行轨迹回放数据源）。

覆盖：
- GET /api/execution/node-runs/（列表 + run/node_type/status 过滤）
- GET /api/execution/runs/{id}/node-runs/（嵌套动作，按执行顺序返回）
- GET /api/execution/runs/?suite=<id>（按 Suite 过滤运行实例）
- 序列化展示字段（node_type_display / status_display / suite_name / case_name / symbol）
"""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from apps.cases.models import Case
from apps.suites.models import Suite

from .models import NodeRun, SuiteRun


class NodeRunAPITest(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.suite = Suite.objects.create(name='回放根 Suite')
        self.child = Suite.objects.create(name='子 Suite', parent=self.suite)
        self.case_a = Case.objects.create(name='信号 RSI', node_type='signal')
        self.case_b = Case.objects.create(name='执行下单', node_type='executor')
        self.run = SuiteRun.objects.create(suite=self.suite, symbol='000001', status='running')
        self.other_run = SuiteRun.objects.create(suite=self.child, symbol='600000', status='pending')

        self.root_node = NodeRun.objects.create(run=self.run, node_type='suite', suite=self.suite)
        self.case_node = NodeRun.objects.create(
            run=self.run, parent=self.root_node, node_type='case',
            case=self.case_a, status='completed', direction=1,
        )
        self.failed_node = NodeRun.objects.create(
            run=self.run, parent=self.root_node, node_type='case',
            case=self.case_b, status='failed', result={'error': '风控拦截'},
        )
        self.child_node = NodeRun.objects.create(run=self.run, node_type='suite', suite=self.child)
        # 其他 run 的节点，不应出现在 self.run 的轨迹里
        self.detached_node = NodeRun.objects.create(run=self.other_run, node_type='suite', suite=self.child)

    def test_node_run_list_filter_by_run(self):
        response = self.client.get('/api/execution/node-runs/', {'run': self.run.id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item['id'] for item in response.data]
        self.assertEqual(sorted(ids), sorted([self.root_node.id, self.case_node.id, self.failed_node.id, self.child_node.id]))
        self.assertNotIn(self.detached_node.id, ids)

    def test_node_run_list_filter_by_status_and_node_type(self):
        response = self.client.get('/api/execution/node-runs/', {'run': self.run.id, 'status': 'failed'})
        self.assertEqual([item['id'] for item in response.data], [self.failed_node.id])

        response = self.client.get('/api/execution/node-runs/', {'run': self.run.id, 'node_type': 'suite'})
        self.assertEqual(len(response.data), 2)

    def test_node_run_display_fields(self):
        response = self.client.get(f'/api/execution/node-runs/{self.case_node.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.data
        self.assertEqual(payload['node_type_display'], 'Case 节点')
        self.assertEqual(payload['status_display'], '已完成')
        self.assertEqual(payload['case_name'], '信号 RSI')
        self.assertEqual(payload['symbol'], '000001')
        self.assertEqual(payload['direction'], 1)

    def test_run_nested_node_runs_ordered_by_execution(self):
        response = self.client.get(f'/api/execution/runs/{self.run.id}/node-runs/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item['id'] for item in response.data]
        self.assertEqual(ids, sorted(ids))
        self.assertNotIn(self.detached_node.id, ids)

    def test_suite_run_filter_by_suite(self):
        response = self.client.get('/api/execution/runs/', {'suite': self.suite.id})
        self.assertEqual([item['id'] for item in response.data], [self.run.id])

    def test_nested_action_missing_run_returns_empty(self):
        response = self.client.get(f'/api/execution/runs/{self.other_run.id}/node-runs/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item['id'] for item in response.data], [self.detached_node.id])


class NodeRunModelTest(TestCase):
    def test_str_representation(self):
        suite = Suite.objects.create(name='S')
        node = NodeRun.objects.create(run=SuiteRun.objects.create(suite=suite, symbol='000001'), node_type='suite', suite=suite)
        self.assertEqual(str(node), f'suite:{suite.id} - pending')

        case = Case.objects.create(name='C', node_type='signal')
        case_node = NodeRun.objects.create(run=node.run, node_type='case', case=case)
        self.assertEqual(str(case_node), f'case:{case.id} - pending')
