"""suites 编排增强测试（S-09/S-10/S-11）：

- 子 Suite 递归执行与 NodeRun 轨迹
- 树形运行时聚合（子 Suite 方向汇总到父节点）
- Suite 级并行分支执行与 join 汇合
- fail_stop 失败传播
- 发布快照驱动的执行一致性
"""

from django.test import TestCase

from apps.cases.models import Case
from apps.execution.models import ExecutionLog, NodeRun
from apps.plans.models import Plan
from apps.suites.models import Edge, Suite, SuiteVersion
from apps.suites.services import publish_suite

from .engine import SuiteRunner


def make_case(name, direction, event_type='SUITE_INIT', weight=None):
    params = {
        'trigger': {'event_type': event_type},
        'result': {'direction': direction},
    }
    if weight is not None:
        params['weight'] = weight
    return Case.objects.create(
        name=name, node_type='signal', status='published', params=params,
    )


class OrchestrationTestBase(TestCase):
    def setUp(self):
        self.root = Suite.objects.create(name='根 Suite', status='published')
        self.plan = Plan.objects.create(
            name='编排 Plan', root_suite=self.root, status='published',
            symbol_scope={'type': 'symbols'},
        )

    def add_child(self, name, aggregate_method='weighted_sum', cases=(),
                  event_condition=None, weight=1.0):
        child = Suite.objects.create(name=name, status='published',
                                     aggregate_method=aggregate_method,
                                     parent=self.root)
        for case in cases:
            child.cases.add(case)
        if event_condition is not None:
            Edge.objects.create(
                from_suite=self.root, to_suite=child,
                event_condition=event_condition, weight=weight,
            )
        return child


class SubSuiteRecursiveTest(OrchestrationTestBase):
    def test_root_routes_into_child_and_executes_recursively(self):
        root_case = make_case('根信号', 1)
        child_case = make_case('子信号', -1, event_type='CASE_START')
        self.root.cases.add(root_case)
        child = self.add_child('下游', cases=[child_case],
                               event_condition={'event_type': 'CASE_COMPLETED'})

        log = SuiteRunner().run(self.plan, '000010')

        self.assertEqual(log.status, 'success')
        # 根节点聚合 = root case(+1) + 子 Suite 分支(-1) 汇合抵消
        self.assertEqual(log.final_direction, 0)
        # NodeRun 轨迹：根节点 + 子 Suite 节点 + 两个 Case 节点
        suite_runs = NodeRun.objects.filter(run__symbol='000010', node_type='suite')
        self.assertTrue(suite_runs.filter(suite=child, direction=-1).exists())
        self.assertTrue(suite_runs.filter(suite=self.root).exists())
        self.assertTrue(
            NodeRun.objects.filter(node_type='case', case=child_case, direction=-1).exists()
        )

    def test_grandchild_executes_through_event_queue(self):
        root_case = make_case('根信号', 1)
        self.root.cases.add(root_case)
        middle = self.add_child('中层', event_condition={'event_type': 'CASE_COMPLETED'})
        leaf_case = make_case('叶子信号', 1, event_type='CASE_START')
        leaf = Suite.objects.create(name='叶子', status='published', parent=middle)
        leaf.cases.add(leaf_case)
        Edge.objects.create(
            from_suite=middle, to_suite=leaf,
            event_condition={'event_type': 'CASE_COMPLETED'},
        )

        log = SuiteRunner().run(self.plan, '000011')

        self.assertEqual(log.final_direction, 1)
        self.assertTrue(NodeRun.objects.filter(node_type='suite', suite=leaf).exists())


class TreeAggregationTest(OrchestrationTestBase):
    def test_child_directions_aggregate_into_parent(self):
        root_case = make_case('根信号', 0)
        self.root.cases.add(root_case)
        # 两个分支方向相反、权重均为 1 -> join 汇合后相互抵消
        first = self.add_child('多分支', cases=[make_case('多信号', 1, 'CASE_START')],
                               event_condition={'event_type': 'CASE_COMPLETED'})
        second = self.add_child('空分支', cases=[make_case('空信号', -1, 'CASE_START')],
                                event_condition={'event_type': 'CASE_COMPLETED'})

        log = SuiteRunner().run(self.plan, '000012')

        self.assertEqual(log.status, 'success')
        self.assertTrue(
            NodeRun.objects.filter(node_type='suite', suite=first, direction=1).exists())
        self.assertTrue(
            NodeRun.objects.filter(node_type='suite', suite=second, direction=-1).exists())
        self.assertEqual(log.final_direction, 0)

    def test_edge_weight_influences_join_result(self):
        root_case = make_case('根信号', 0)
        self.root.cases.add(root_case)
        self.add_child('重分支', cases=[make_case('重信号', 1, 'CASE_START')],
                       event_condition={'event_type': 'CASE_COMPLETED'}, weight=3.0)
        self.add_child('轻分支', cases=[make_case('轻信号', -1, 'CASE_START')],
                       event_condition={'event_type': 'CASE_COMPLETED'}, weight=1.0)

        log = SuiteRunner().run(self.plan, '000013')

        # 3 * 1 + 1 * (-1) = 2 > 0 -> 加权聚合为 1
        self.assertEqual(log.final_direction, 1)


class ParallelJoinTest(OrchestrationTestBase):
    def test_parallel_mode_joins_multiple_branches(self):
        self.plan.exec_mode = 'parallel'
        self.plan.save()
        root_case = make_case('根信号', 0)
        self.root.cases.add(root_case)
        self.add_child('分支A', cases=[make_case('A信号', 1, 'CASE_START')],
                       event_condition={'event_type': 'CASE_COMPLETED'})
        self.add_child('分支B', cases=[make_case('B信号', 1, 'CASE_START')],
                       event_condition={'event_type': 'CASE_COMPLETED'})

        # use_threads=False 保证 SQLite 测试下确定性，join 逻辑与线程路径一致
        log = SuiteRunner(use_threads=False).run(self.plan, '000014')

        self.assertEqual(log.status, 'success')
        self.assertEqual(log.final_direction, 1)


class FailStopTest(OrchestrationTestBase):
    def test_fail_stop_propagates_case_failure(self):
        self.plan.exec_mode = 'fail_stop'
        self.plan.save()
        bad_case = Case.objects.create(
            name='坏信号', node_type='signal', status='published',
            params={'trigger': {'event_type': 'SUITE_INIT'}, 'result': 'invalid'},
        )
        self.root.cases.add(bad_case)

        with self.assertRaises(Exception):
            SuiteRunner().run(self.plan, '000015')

        self.assertEqual(ExecutionLog.objects.get(symbol='000015').status, 'failed')
        self.assertTrue(
            NodeRun.objects.filter(node_type='case', case=bad_case, status='failed').exists())


class SnapshotDrivenExecutionTest(OrchestrationTestBase):
    def test_runner_uses_published_snapshot_over_live_topology(self):
        root_case = make_case('根信号', 1)
        self.root.cases.add(root_case)
        child = self.add_child('快照下游', cases=[make_case('下游信号', -1, 'CASE_START')],
                               event_condition={'event_type': 'CASE_COMPLETED'})
        publish_suite(self.root)
        self.assertTrue(SuiteVersion.objects.filter(suite=self.root).exists())

        # 发布后偷偷改动实时拓扑：新增一条不应生效的边
        stale = Suite.objects.create(name='过期下游', status='published', parent=self.root)
        stale.cases.add(make_case('过期信号', 1, 'CASE_START'))
        Edge.objects.create(
            from_suite=self.root, to_suite=stale,
            event_condition={'event_type': 'CASE_COMPLETED'},
        )
        # 并清空快照中已有下游 Case 的实时成员关系（实时已被破坏）
        child.cases.clear()

        log = SuiteRunner().run(self.plan, '000016')

        # 引擎按快照执行：下游节点仍被执行
        self.assertEqual(log.status, 'success')
        self.assertTrue(NodeRun.objects.filter(node_type='suite', suite=child).exists())
        # 过期边不在快照内，不触发执行
        self.assertFalse(NodeRun.objects.filter(node_type='suite', suite=stale).exists())