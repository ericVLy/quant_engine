from django.db import transaction

from .models import Edge, Suite


class SuiteError(Exception):
    """Raised when a Suite cannot be changed or published."""


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
    suite.status = 'published'
    suite.version += 1
    suite.save(update_fields=('status', 'version', 'updated_at'))
    return suite


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
        edge_records.append({
            'to_suite_id': to_id,
            'condition': edge_data.get('condition') or {},
            'event_condition': edge_data.get('event_condition') or {},
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
