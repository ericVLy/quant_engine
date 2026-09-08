"""Suite 边条件操作符 + 拓扑完整性校验 测试。"""
from django.test import TestCase

from apps.suites.models import Suite, Edge
from apps.suites.services import (
    event_condition_matches,
    validate_event_condition_obj,
    validate_topology,
    SuiteError,
)


class EventConditionOperatorMatchTest(TestCase):
    def test_eq(self):
        self.assertTrue(event_condition_matches(
            {'field': 'price', 'op': 'eq', 'threshold': 100}, {'price': 100}))
        self.assertFalse(event_condition_matches(
            {'field': 'price', 'op': 'eq', 'threshold': 100}, {'price': 101}))

    def test_neq(self):
        self.assertTrue(event_condition_matches(
            {'field': 'price', 'op': 'neq', 'threshold': 100}, {'price': 101}))
        self.assertFalse(event_condition_matches(
            {'field': 'price', 'op': 'neq', 'threshold': 100}, {'price': 100}))

    def test_gt_gte_lt_lte(self):
        self.assertTrue(event_condition_matches(
            {'field': 'vol', 'op': 'gt', 'threshold': 1000}, {'vol': 1001}))
        self.assertTrue(event_condition_matches(
            {'field': 'vol', 'op': 'gte', 'threshold': 1000}, {'vol': 1000}))
        self.assertFalse(event_condition_matches(
            {'field': 'vol', 'op': 'gt', 'threshold': 1000}, {'vol': 1000}))
        self.assertTrue(event_condition_matches(
            {'field': 'vol', 'op': 'lt', 'threshold': 1000}, {'vol': 999}))
        self.assertTrue(event_condition_matches(
            {'field': 'vol', 'op': 'lte', 'threshold': 1000}, {'vol': 1000}))

    def test_between_inclusive(self):
        cond = {'field': 'price', 'op': 'between', 'threshold': [50, 150]}
        self.assertTrue(event_condition_matches(cond, {'price': 50}))
        self.assertTrue(event_condition_matches(cond, {'price': 150}))
        self.assertFalse(event_condition_matches(cond, {'price': 49}))
        self.assertFalse(event_condition_matches(cond, {'price': 151}))

    def test_empty_condition_matches_all(self):
        self.assertTrue(event_condition_matches({}, {'anything': 1}))
        self.assertTrue(event_condition_matches(None, {}))

    def test_legacy_key_eq_still_works(self):
        self.assertTrue(event_condition_matches({'event_type': 'X'}, {'event_type': 'X'}))
        self.assertFalse(event_condition_matches({'event_type': 'X'}, {'event_type': 'Y'}))

    def test_non_numeric_field_for_compare_ops_returns_false(self):
        self.assertFalse(event_condition_matches(
            {'field': 'name', 'op': 'gt', 'threshold': 10}, {'name': 'abc'}))
class ValidateOperatorObjTest(TestCase):
    def test_valid_between(self):
        self.assertEqual(
            validate_event_condition_obj({'event_type': 'X', 'op': 'between', 'field': 'p', 'threshold': [1, 5]}),
            {'event_type': 'X', 'op': 'between', 'field': 'p', 'threshold': [1, 5]})

    def test_op_without_field_rejected(self):
        with self.assertRaises(SuiteError):
            validate_event_condition_obj({'event_type': 'X', 'op': 'gt', 'threshold': 1})

    def test_unknown_op_rejected(self):
        with self.assertRaises(SuiteError):
            validate_event_condition_obj(
                {'event_type': 'X', 'op': 'like', 'field': 'p', 'threshold': 1})

    def test_between_wrong_length_rejected(self):
        with self.assertRaises(SuiteError):
            validate_event_condition_obj(
                {'event_type': 'X', 'op': 'between', 'field': 'p', 'threshold': [1, 2, 3]})

    def test_between_lo_gt_hi_rejected(self):
        with self.assertRaises(SuiteError):
            validate_event_condition_obj(
                {'event_type': 'X', 'op': 'between', 'field': 'p', 'threshold': [10, 1]})

    def test_threshold_bool_rejected(self):
        with self.assertRaises(SuiteError):
            validate_event_condition_obj(
                {'event_type': 'X', 'op': 'gt', 'field': 'p', 'threshold': True})


class TopologyValidationTest(TestCase):
    def setUp(self):
        self.root = Suite.objects.create(name='root', aggregate_method='weighted_sum')
        self.child_a = Suite.objects.create(name='child_a', aggregate_method='weighted_sum', parent=self.root)
        self.child_b = Suite.objects.create(name='child_b', aggregate_method='weighted_sum', parent=self.root)

    def test_valid_topology_passes(self):
        Edge.objects.create(from_suite=self.root, to_suite=self.child_a, weight=1.0)
        Edge.objects.create(from_suite=self.child_a, to_suite=self.child_b, weight=1.0)
        self.assertTrue(validate_topology(self.root))

    def test_cross_tree_edge_detected(self):
        other = Suite.objects.create(name='other')
        Edge.objects.create(from_suite=self.root, to_suite=other, weight=1.0)
        with self.assertRaises(SuiteError):
            validate_topology(self.root)

    def test_duplicate_edge_detected(self):
        # DB unique_together 已防字面重复；这里验证 event_condition 不同但 from→to 相同时仍被语义去重
        # 用不同 condition 创建两条 from→to 相同但不完全相同的边（约束允许），validate_topology 应报重复
        Edge.objects.create(from_suite=self.root, to_suite=self.child_a, weight=1.0, condition={'a': 1})
        Edge.objects.create(from_suite=self.root, to_suite=self.child_a, weight=2.0, condition={'b': 2})
        with self.assertRaises(SuiteError):
            validate_topology(self.root)

    def test_invalid_weight_detected(self):
        Edge.objects.create(from_suite=self.root, to_suite=self.child_a, weight=0)
        with self.assertRaises(SuiteError):
            validate_topology(self.root)

    def test_isolated_node_detected(self):
        # child_b 有入边，child_a 无入边 → 孤立
        Edge.objects.create(from_suite=self.root, to_suite=self.child_b, weight=1.0)
        with self.assertRaises(SuiteError):
            validate_topology(self.root)