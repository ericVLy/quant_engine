from django.db import transaction

from .models import Edge, Suite, SuiteVersion


class SuiteError(Exception):
    """Raised when a Suite cannot be changed or published."""


def validate_event_condition_obj(value):
    """Strictly validate event_condition JSON for topology edges."""
    if not isinstance(value, dict):
        raise SuiteError('event_condition 必须是 JSON 对象')

    allowed_keys = {'event_type', 'case_id', 'next_event'}
    unknown = set(value.keys()) - allowed_keys
    if unknown:
        raise SuiteError(f'event_condition 不允许的字段: {", ".join(sorted(unknown))}')

    if 'event_type' not in value or not value['event_type']:
        raise SuiteError('event_condition.event_type 是必填字段')

    if 'case_id' in value and (not isinstance(value['case_id'], int) or isinstance(value['case_id'], bool)):
        raise SuiteError('event_condition.case_id 必须是整数')

    if 'next_event' in value and (not isinstance(value['next_event'], str) or not value['next_event']):
        raise SuiteError('event_condition.next_event 必须是非空字符串')

    return value


def event_condition_matches(condition, payload):
    """Return whether every configured condition equals the event payload."""
    if not condition:
        return True
    return all(payload.get(key) == value for key, value in condition.items())


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


def validate_publishable(suite):
    """Validate DAG and all Cases/Suite descendants before publishing."""
    validate_dag(suite)
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
