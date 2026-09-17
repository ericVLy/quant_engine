"""MCP tool implementations — thin facades over existing Django services."""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from django.db.models import Count, Q

from mcp_server.formatting import to_jsonable


def _symbol_brief(symbol) -> dict[str, Any]:
    return {
        'id': symbol.id,
        'code': symbol.code,
        'name': symbol.name,
        'market': symbol.market,
        'exchange': symbol.exchange or '',
    }


def search_symbols(query: str = '', market: str = '', limit: int = 50) -> dict[str, Any]:
    from apps.watchlists.models import Symbol

    limit = max(1, min(int(limit), 200))
    qs = Symbol.objects.all().order_by('code')
    if market:
        qs = qs.filter(market=str(market).upper())
    q = (query or '').strip()
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
    rows = [_symbol_brief(s) for s in qs[:limit]]
    return {'count': len(rows), 'symbols': rows}


def resolve_symbol_name(code: str, market: str = '') -> dict[str, Any]:
    from apps.watchlists.models import Symbol
    from apps.watchlists.services import resolve_symbol_name as lookup_name

    code = (code or '').strip()
    if not code:
        raise ValueError('code 不能为空')
    symbol = Symbol.objects.filter(code=code).first()
    if symbol is None and market:
        symbol = Symbol.objects.filter(code=code, market=str(market).upper()).first()
    name = symbol.name if symbol else lookup_name(code, market or None)
    return {
        'code': code,
        'name': name,
        'symbol': _symbol_brief(symbol) if symbol else None,
    }


def query_kline(
    symbol_code: str,
    start_date: str = '',
    end_date: str = '',
    limit: int = 120,
) -> dict[str, Any]:
    from apps.datasources.services import query_kline_table
    from apps.watchlists.models import Symbol

    code = (symbol_code or '').strip()
    if not code:
        raise ValueError('symbol_code 不能为空')
    symbol = Symbol.objects.filter(code=code).first()
    if symbol is None:
        raise ValueError(f'未找到标的: {code}')

    today = date.today()
    end = date.fromisoformat(end_date) if end_date else today
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=90)
    if start > end:
        raise ValueError('start_date 不能晚于 end_date')

    rows = query_kline_table(symbol, start, end)
    limit = max(1, min(int(limit), 500))
    if len(rows) > limit:
        rows = rows[-limit:]
    return {
        'symbol': code,
        'market': symbol.market,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'count': len(rows),
        'bars': to_jsonable(rows),
    }


def list_plans(status: str = 'published', limit: int = 50) -> dict[str, Any]:
    from apps.plans.models import Plan

    limit = max(1, min(int(limit), 200))
    qs = Plan.objects.select_related('root_suite').order_by('-updated_at')
    if status:
        qs = qs.filter(status=status)
    plans = []
    for plan in qs[:limit]:
        plans.append({
            'id': plan.id,
            'name': plan.name,
            'status': plan.status,
            'version': plan.version,
            'trigger_type': plan.trigger_type,
            'exec_mode': plan.exec_mode,
            'run_status': plan.run_status,
            'root_suite_id': plan.root_suite_id,
            'root_suite_name': plan.root_suite.name if plan.root_suite_id else '',
        })
    return {'count': len(plans), 'plans': plans}


def get_plan(plan_id: int, include_symbols: bool = True) -> dict[str, Any]:
    from apps.plans.models import Plan
    from apps.plans.services import resolve_plan_symbols

    plan = Plan.objects.select_related('root_suite').filter(pk=plan_id).first()
    if plan is None:
        raise ValueError(f'Plan 不存在: {plan_id}')
    data: dict[str, Any] = {
        'id': plan.id,
        'name': plan.name,
        'status': plan.status,
        'version': plan.version,
        'trigger_type': plan.trigger_type,
        'cron_expr': plan.cron_expr,
        'event_type': plan.event_type,
        'symbol_scope': plan.symbol_scope or {},
        'exec_mode': plan.exec_mode,
        'retry_policy': plan.retry_policy or {},
        'run_status': plan.run_status,
        'root_suite': {
            'id': plan.root_suite_id,
            'name': plan.root_suite.name,
            'status': plan.root_suite.status,
        },
    }
    if include_symbols:
        symbols = resolve_plan_symbols(plan)
        data['symbols'] = [_symbol_brief(s) for s in symbols[:500]]
        data['symbol_count'] = symbols.count()
    return data


def list_cases(status: str = '', node_type: str = '', limit: int = 50) -> dict[str, Any]:
    from apps.cases.models import Case

    limit = max(1, min(int(limit), 200))
    qs = Case.objects.order_by('-updated_at')
    if status:
        qs = qs.filter(status=status)
    if node_type:
        qs = qs.filter(node_type=node_type)
    cases = [
        {
            'id': c.id,
            'name': c.name,
            'node_type': c.node_type,
            'status': c.status,
            'version': c.version,
            'run_status': c.run_status,
        }
        for c in qs[:limit]
    ]
    return {'count': len(cases), 'cases': cases}


def get_case(case_id: int) -> dict[str, Any]:
    from apps.cases.models import Case

    case = Case.objects.filter(pk=case_id).first()
    if case is None:
        raise ValueError(f'Case 不存在: {case_id}')
    return to_jsonable({
        'id': case.id,
        'name': case.name,
        'node_type': case.node_type,
        'status': case.status,
        'version': case.version,
        'run_status': case.run_status,
        'params': case.params or {},
    })


def get_suite_topology(suite_id: int) -> dict[str, Any]:
    from apps.suites.models import Suite
    from apps.suites.services import build_topology_snapshot

    suite = Suite.objects.filter(pk=suite_id).first()
    if suite is None:
        raise ValueError(f'Suite 不存在: {suite_id}')
    return {
        'id': suite.id,
        'name': suite.name,
        'status': suite.status,
        'version': suite.version,
        'topology': build_topology_snapshot(suite),
    }


def list_event_types(include_system: bool = True) -> dict[str, Any]:
    from apps.execution.registry import EventRegistry

    items = EventRegistry.list_all(include_system=include_system)
    return {'count': len(items), 'event_types': items}


def list_alerts(
    status: str = '',
    severity: str = '',
    limit: int = 30,
) -> dict[str, Any]:
    from apps.execution.models import Alert

    limit = max(1, min(int(limit), 100))
    qs = Alert.objects.select_related('plan').order_by('-created_at')
    if status:
        qs = qs.filter(status=status)
    if severity:
        qs = qs.filter(severity=severity)
    alerts = []
    for alert in qs[:limit]:
        alerts.append({
            'id': alert.id,
            'alert_type': alert.alert_type,
            'severity': alert.severity,
            'status': alert.status,
            'title': alert.title,
            'plan_id': alert.plan_id,
            'created_at': alert.created_at.isoformat() if alert.created_at else None,
        })
    return {'count': len(alerts), 'alerts': alerts}


def alert_statistics() -> dict[str, Any]:
    from apps.execution.models import Alert

    stats = Alert.objects.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status='pending')),
        acknowledged=Count('id', filter=Q(status='acknowledged')),
        resolved=Count('id', filter=Q(status='resolved')),
        high_severity=Count('id', filter=Q(severity='high')),
        critical_severity=Count('id', filter=Q(severity='critical')),
    )
    by_type = list(
        Alert.objects.values('alert_type').annotate(count=Count('id')).order_by('-count')
    )
    return {'overview': stats, 'by_type': by_type}


def get_intraday_series(symbol_code: str, limit: int = 240) -> dict[str, Any]:
    from apps.monitoring.market_calendar import MARKET_TIMEZONES, session_status, to_market_local
    from apps.monitoring.models import IntradayPoint
    from apps.monitoring.serializers import IntradayPointSerializer
    from apps.watchlists.models import Symbol
    from django.utils import timezone

    code = (symbol_code or '').strip()
    if not code:
        raise ValueError('symbol_code 不能为空')
    symbol = Symbol.objects.filter(code=code).first()
    if symbol is None:
        raise ValueError(f'未找到标的: {code}')

    points = list(
        IntradayPoint.objects.filter(symbol=symbol).order_by('ts')
    )
    limit = max(1, min(int(limit), 500))
    if len(points) > limit:
        points = points[-limit:]
    now_local = to_market_local(timezone.now(), symbol.market)
    latest = points[-1] if points else None
    pre_close = str(latest.pre_close) if latest and latest.pre_close is not None else None
    serialized = IntradayPointSerializer(points, many=True).data
    return {
        'symbol': symbol.code,
        'market': symbol.market,
        'timezone': MARKET_TIMEZONES[symbol.market],
        'session_status': session_status(symbol.market, now_local),
        'pre_close': pre_close,
        'count': len(serialized),
        'points': serialized,
    }


def list_suite_runs(plan_id: int = 0, symbol: str = '', limit: int = 20) -> dict[str, Any]:
    from apps.execution.models import SuiteRun

    limit = max(1, min(int(limit), 100))
    qs = SuiteRun.objects.select_related('plan', 'suite').order_by('-started_at', '-id')
    if plan_id:
        qs = qs.filter(plan_id=int(plan_id))
    if symbol:
        qs = qs.filter(symbol=str(symbol).strip())
    runs = []
    for run in qs[:limit]:
        runs.append({
            'id': run.id,
            'plan_id': run.plan_id,
            'suite_id': run.suite_id,
            'symbol': run.symbol,
            'status': run.status,
            'started_at': run.started_at.isoformat() if run.started_at else None,
            'ended_at': run.ended_at.isoformat() if run.ended_at else None,
        })
    return {'count': len(runs), 'runs': runs}


def trigger_plan_execution(plan_id: int, symbols: list[str]) -> dict[str, Any]:
    if os.getenv('MCP_ALLOW_TRIGGER', '').strip().lower() not in ('1', 'true', 'yes'):
        raise PermissionError(
            'MCP 写操作已禁用。设置环境变量 MCP_ALLOW_TRIGGER=1，'
            '或通过命令行参数 --allow-trigger 启动 MCP 服务后重试。'
        )
    from apps.execution.services import trigger_plan

    if not symbols:
        raise ValueError('symbols 不能为空')
    runs = trigger_plan(plan_id, [str(s).strip() for s in symbols if str(s).strip()])
    return {
        'plan_id': plan_id,
        'created_run_ids': [run.id for run in runs],
        'count': len(runs),
    }


def system_overview_text() -> str:
    return (
        'Quant Engine：本地优先的量化投研与交易系统。\n'
        'Django 负责数据与 REST API；runner 独立进程负责 Plan 调度与 Suite 事件循环。\n'
        '核心隐喻：Case（原子策略）→ Suite（DAG 编排）→ Plan（调度）→ SuiteRun/Order。\n'
        'MCP 服务默认以 SSE（HTTP）传输暴露标的、K 线、策略元数据、告警与分时等只读视图；\n'
        '触发 Plan 需 MCP_ALLOW_TRIGGER=1（或启动参数 --allow-trigger），且只创建 pending SuiteRun，不会直接下单。\n'
    )
