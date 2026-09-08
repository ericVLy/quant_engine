from django.db import transaction

from .models import Edge, Suite, SuiteVersion


class SuiteError(Exception):
    """Raised when a Suite cannot be changed or published."""


def validate_event_condition_obj(value):
    """Strictly validate event_condition JSON for topology edges."""
    if not isinstance(value, dict):
        raise SuiteError('event_condition 必须是 JSON 对象')

    allowed_keys = {'event_type', 'case_id', 'next_event', 'op', 'field', 'threshold'}
    unknown = set(value.keys()) - allowed_keys
    if unknown:
        raise SuiteError(f'event_condition 不允许的字段: {", ".join(sorted(unknown))}')

    if 'event_type' not in value or not value['event_type']:
        raise SuiteError('event_condition.event_type 是必填字段')

    if 'case_id' in value and (not isinstance(value['case_id'], int) or isinstance(value['case_id'], bool)):
        raise SuiteError('event_condition.case_id 必须是整数')

    if 'next_event' in value and (not isinstance(value['next_event'], str) or not value['next_event']):
        raise SuiteError('event_condition.next_event 必须是非空字符串')

    _validate_operator_obj(value)
    return value


def _validate_operator_obj(value):
    allowed_ops = {'eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between'}
    present = {k for k in ('op', 'field', 'threshold') if k in value}
    if not present:
        if 'field' in value or 'threshold' in value:
            raise SuiteError('提供 field/threshold 时必须同时提供 op')
        return value
    if present != {'op', 'field', 'threshold'}:
        raise SuiteError('op / field / threshold 必须同时提供')
    op = value['op']
    if op not in allowed_ops:
        raise SuiteError(f'不允许的操作符: {op}（允许 {sorted(allowed_ops)}）')
    if not value['field'] or not isinstance(value['field'], str):
        raise SuiteError('event_condition.field 必须是非空字符串')
    threshold = value['threshold']
    if op == 'between':
        if not (isinstance(threshold, (list, tuple)) and len(threshold) == 2):
            raise SuiteError('between 操作符的 threshold 必须是双元素数组 [低, 高]')
        lo, hi = threshold
        if not isinstance(lo, (int, float)) or isinstance(lo, bool) or \
           not isinstance(hi, (int, float)) or isinstance(hi, bool):
            raise SuiteError('between 的边界必须是数值')
        if lo > hi:
            raise SuiteError('between 的低边界不能大于高边界')
    elif not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise SuiteError('threshold 必须是数值')
    return value


def event_condition_matches(condition, payload):
    """Return whether every configured condition equals/routes the event payload.

    支持两种匹配模式：
    1. 简单键值相等：键直接与 payload 比对；
    2. 操作符契约：{field, op, threshold} 对 payload[field] 做数值比较。
    """
    if not condition:
        return True

    op = condition.get('op')
    if op:
        return _apply_operator(condition, payload)

    # 兼容旧契约：键值相等
    return all(payload.get(key) == value for key, value in condition.items())


def _apply_operator(condition, payload):
    """按 op 对 payload[field] 与 threshold 做比较。"""
    op = condition.get('op')
    field = condition.get('field')
    threshold = condition.get('threshold')
    actual = payload.get(field)

    # 数值比较需可转 float
    try:
        lhs = float(actual)
    except (TypeError, ValueError):
        lhs = actual  # 非数值字段按原样比较（仅 eq/neq 有意义）

    if op == 'eq':
        return lhs == threshold or actual == threshold
    if op == 'neq':
        return not (lhs == threshold or actual == threshold)
    if op in ('gt', 'gte', 'lt', 'lte'):
        if not isinstance(lhs, (int, float)) or isinstance(lhs, bool):
            return False
        if op == 'gt':
            return lhs > threshold
        if op == 'gte':
            return lhs >= threshold
        if op == 'lt':
            return lhs < threshold
        return lhs <= threshold
    if op == 'between':
        if not isinstance(lhs, (int, float)) or isinstance(lhs, bool):
            return False
        return threshold[0] <= lhs <= threshold[1]
    return False


def aggregate_directions(suite, results):
    """Aggregate Case directions according to the Suite configuration."""
    if not results:
        return 0
    directions = [int(result.get('direction', 0)) for result in results]
    if suite.aggregate_method == 'and':
        return 1 if all(direction == 1 for direction in directions) else -1 if any(direction == -1 for direction in directions) else 0
    if suite.aggregate_method == 'or':
        return 1 if any(direction == 1 for direction in directions) else -1 if any(direction == -1 for direction in directions) else 0
    if suite.aggregate_method == 'vote':
        totals = {direction: directions.count(direction) for direction in (-1, 0, 1)}
        return max(totals, key=totals.get)
    weighted = sum(
        direction * float(result.get('weight', 1.0))
        for direction, result in zip(directions, results)
    )
    return 1 if weighted > 0 else -1 if weighted < 0 else 0


def validate_dag(suite):
    """Validate that Suite edges reachable from suite contain no cycle."""
    visiting = set()
    visited = set()

    def visit(current):
        if current.pk in visiting:
            raise SuiteError('Suite 拓扑存在环路')
        if current.pk in visited:
            return

        visiting.add(current.pk)
        for edge in Edge.objects.filter(from_suite=current).select_related('to_suite'):
            visit(edge.to_suite)
        visiting.remove(current.pk)
        visited.add(current.pk)

    visit(suite)
    return True


def validate_topology(suite):
    """Suite 拓扑完整性校验（发布前调用）。

    覆盖：跨树入边、重复边、非法权重、不可达节点、孤立节点。
    """
    root_id = suite.pk
    from_suites = set(Edge.objects.filter(from_suite_id=root_id).values_list('from_suite_id', flat=True))

    # 1. 所有出边必须属于本树（from_suite 只能在本树内）
    all_edges = Edge.objects.filter(
        from_suite_id__in=_collect_suite_ids(suite),
    ).select_related('from_suite', 'to_suite')
    tree_ids = _collect_suite_ids(suite)

    for edge in all_edges:
        if edge.to_suite_id not in tree_ids:
            raise SuiteError(
                f'跨树入边：Edge {edge.from_suite.name} → {edge.to_suite.name} '
                f'指向本 Suite 树之外的 Suite'
            )
        if not (0 < edge.weight <= 1000):
            raise SuiteError(
                f'非法权重 {edge.weight}（Edge {edge.from_suite_id} → {edge.to_suite_id}），必须 > 0'
            )

    # 2. 重复出边（同 from → to 出现多次）
    seen = set()
    for edge in all_edges:
        key = (edge.from_suite_id, edge.to_suite_id)
        if key in seen:
            raise SuiteError(f'重复边：{edge.from_suite_id} → {edge.to_suite_id}')
        seen.add(key)

    # 3. 不可达节点（树内除根以外的节点无法从根到达）
    _validate_reachable(suite)

    detect_isolated_nodes(suite)
    return True


def _collect_suite_ids(suite):
    """收集根 Suite 及其所有后代（子 Suite）的 id。"""
    ids = {suite.pk}
    pending = list(suite.children.all())
    seen = {suite.pk}
    while pending:
        child = pending.pop()
        if child.pk in seen:
            continue
        seen.add(child.pk)
        ids.add(child.pk)
        pending.extend(child.children.all())
    return ids


def _validate_reachable(suite):
    """校验树内每个节点都能从根通过有向边到达（根自身除外）。"""
    reachable = {suite.pk}
    frontier = [suite.pk]
    while frontier:
        cur = frontier.pop()
        for edge in Edge.objects.filter(from_suite_id=cur):
            if edge.to_suite_id not in reachable:
                reachable.add(edge.to_suite_id)
                frontier.append(edge.to_suite_id)
    tree_ids = _collect_suite_ids(suite)
    unreachable = tree_ids - reachable - {suite.pk}
    if unreachable:
        raise SuiteError(f'存在不可达节点：{sorted(unreachable)}')


def detect_isolated_nodes(suite):
    """检测孤立节点：树内存在子 Suite，但没有任何一条边指向它（是其父结构要求入边的除外）。"""
    tree_ids = _collect_suite_ids(suite)
    if len(tree_ids) <= 1:
        return  # 只有根节点，无孤立
    incoming_targets = set(Edge.objects.filter(from_suite_id__in=tree_ids).values_list('to_suite_id', flat=True))
    # 根节点无需入边；其余子 Suite 若无入边则视为孤立
    isolated = tree_ids - incoming_targets - {suite.pk}
    from apps.suites.models import Suite as _Suite
    if isolated:
        names = list(_Suite.objects.filter(pk__in=isolated).values_list('name', flat=True))
        raise SuiteError(f'检测到孤立节点（无入边）: {names}')


def validate_publishable(suite):
    """Validate DAG, topology completeness and all Cases/Suite descendants before publishing."""
    validate_dag(suite)
    validate_topology(suite)
    unpublished_cases = suite.cases.exclude(status='published')
    if unpublished_cases.exists():
        raise SuiteError('Suite 包含未发布的 Case')

    descendants = set()
    pending = list(suite.children.all())
    while pending:
        child = pending.pop()
        if child.pk in descendants:
            continue
        descendants.add(child.pk)
        if child.status != 'published':
            raise SuiteError('Suite 包含未发布的子 Suite')
        if child.cases.exclude(status='published').exists():
            raise SuiteError('Suite 包含未发布的 Case')
        pending.extend(child.children.all())
    return True


def publish_suite(suite):
    validate_publishable(suite)
    with transaction.atomic():
        suite.status = 'published'
        suite.version += 1
        suite.save(update_fields=('status', 'version', 'updated_at'))
        # 发布即固化拓扑快照，运行时引擎只读快照（S-09 / SuiteVersion）
        SuiteVersion.objects.create(
            suite=suite, version=suite.version,
            snapshot=build_topology_snapshot(suite),
        )
    return suite


def build_topology_snapshot(suite):
    """递归构建 Suite 编排树的不可变快照（含 Case 成员、出边、子 Suite）。"""
    data = {
        'suite_id': suite.pk,
        'name': suite.name,
        'aggregate_method': suite.aggregate_method,
        'version': suite.version,
        'case_ids': list(suite.cases.values_list('id', flat=True)),
        'cases': [
            {
                'id': case.id, 'name': case.name, 'node_type': case.node_type,
                'status': case.status, 'params': case.params or {},
            }
            for case in suite.cases.all()
        ],
        'edges': [
            {
                'to_suite_id': edge.to_suite_id,
                'condition': edge.condition or {},
                'event_condition': edge.event_condition or {},
                'weight': edge.weight,
            }
            for edge in suite.out_edges.all()
        ],
        'children': [build_topology_snapshot(child) for child in suite.children.all()],
    }
    return data


def update_topology(suite, case_ids, edges):
    """Replace Case memberships and outgoing topology edges atomically."""
    from apps.cases.models import Case

    case_queryset = Case.objects.filter(pk__in=case_ids)
    if case_queryset.count() != len(set(case_ids)):
        raise SuiteError('包含不存在的 Case')

    edge_records = []
    for edge_data in edges:
        from_id = edge_data.get('from_suite')
        to_id = edge_data.get('to_suite')
        if from_id != suite.pk:
            raise SuiteError('拓扑更新只允许修改当前 Suite 的出边')
        if not Suite.objects.filter(pk=to_id).exists():
            raise SuiteError('包含不存在的目标 Suite')
        if to_id == suite.pk:
            raise SuiteError('Suite 不能连接到自身')

        event_condition = edge_data.get('event_condition') or {}
        validate_event_condition_obj(event_condition)

        edge_records.append({
            'to_suite_id': to_id,
            'condition': edge_data.get('condition') or {},
            'event_condition': event_condition,
            'weight': edge_data.get('weight', 1.0),
        })

    with transaction.atomic():
        suite.cases.set(case_queryset)
        Edge.objects.filter(from_suite=suite).delete()
        Edge.objects.bulk_create([
            Edge(from_suite=suite, **edge_data) for edge_data in edge_records
        ])
        validate_dag(suite)
    return suite
