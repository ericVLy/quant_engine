"""MCP tool implementations — thin facades over existing Django services."""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from django.db.models import Count, Q

from mcp_server.formatting import to_jsonable


def _symbol_brief(symbol) -> dict[str, Any]:
    """把 Symbol 实例压成工具输出用的摘要（只保留可公开字段）。

    Args:
        symbol: ``watchlists.models.Symbol`` 实例。

    Returns:
        dict: ``id`` / ``code`` / ``name`` / ``market`` / ``exchange``。
    """
    return {
        'id': symbol.id,
        'code': symbol.code,
        'name': symbol.name,
        'market': symbol.market,
        'exchange': symbol.exchange or '',
    }


def search_symbols(query: str = '', market: str = '', limit: int = 50) -> dict[str, Any]:
    """按代码或名称模糊搜索标的（只读）。

    Args:
        query: 模糊关键字，匹配 ``Symbol.code`` 或 ``Symbol.name``；空串=不过滤关键字。
        market: 市场过滤 ``A`` / ``HK`` / ``US``（大小写不敏感）；空串=全部市场。
        limit: 返回条数上限，收敛到 1~200。

    Returns:
        dict: ``count`` 为本次返回条数；``symbols`` 每项含
        ``id`` / ``code`` / ``name`` / ``market`` / ``exchange``。
    """
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
    """把标的代码解析为中文名称（只读）。

    Args:
        code: 标的代码（如 ``000001``）；空串或纯空白直接报错。
        market: 市场提示 ``A`` / ``HK`` / ``US``；库内未命中时传给
            ``watchlists.services.resolve_symbol_name`` 做回退解析，空串=不限市场。

    Returns:
        dict: ``code`` 去空白后的代码；``name`` 解析出的中文名称；
        ``symbol`` 命中的库内标的摘要（``id`` / ``code`` / ``name`` / ``market`` /
        ``exchange``），未命中为 ``None``。

    Raises:
        ValueError: ``code`` 为空。
    """
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
    """按日期窗口查询单标的 K 线（只读，走 datasources 分表）。

    Args:
        symbol_code: 标的代码，必须已存在于 ``watchlists.Symbol``。
        start_date: 起始日期 ``YYYY-MM-DD``；空串=``end_date`` 前 90 天。
        end_date: 结束日期 ``YYYY-MM-DD``；空串=今天（服务器本地日期）。
        limit: 返回根数上限，收敛到 1~500；超出时保留最近 N 根。

    Returns:
        dict: ``symbol`` / ``market`` / ``start_date`` / ``end_date`` 回显实际查询窗口；
        ``count`` 为返回根数；``bars`` 为逐根 K 线（Decimal→字符串、date→ISO，
        归一逻辑见 :func:`mcp_server.formatting.to_jsonable`）。

    Raises:
        ValueError: 代码为空、标的未入库、日期非法（``date.fromisoformat``）
            或 ``start_date`` 晚于 ``end_date``。
    """
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
    """列出 Plan（只读，按 ``-updated_at`` 排序）。

    Args:
        status: 状态过滤 ``draft`` / ``published`` / ``archived``；空串=全部状态。
        limit: 返回条数上限，收敛到 1~200。

    Returns:
        dict: ``count`` 为本次返回条数；``plans`` 每项含 ``id`` / ``name`` / ``status`` /
        ``version`` / ``trigger_type`` / ``exec_mode`` / ``run_status`` /
        ``root_suite_id`` / ``root_suite_name``。
    """
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
    """读取单个 Plan 详情（只读）。

    Args:
        plan_id: Plan 主键。
        include_symbols: 是否把 ``symbol_scope`` 解析为标的列表；True 时追加
            ``symbols``（最多 500 条）与 ``symbol_count``。

    Returns:
        dict: ``id`` / ``name`` / ``status`` / ``version`` / ``trigger_type`` /
        ``cron_expr`` / ``event_type`` / ``symbol_scope`` / ``exec_mode`` /
        ``retry_policy`` / ``run_status``，以及 ``root_suite``
        （``id`` / ``name`` / ``status``）。

    Raises:
        ValueError: Plan 不存在。
    """
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
    """列出 Case（只读，按 ``-updated_at`` 排序）。

    Args:
        status: 状态过滤 ``draft`` / ``published`` / ``archived``；空串=全部状态。
        node_type: 节点类型过滤 ``signal`` / ``filter`` / ``verdict`` / ``executor``；
            空串=全部节点类型。
        limit: 返回条数上限，收敛到 1~200。

    Returns:
        dict: ``count`` 为本次返回条数；``cases`` 每项含 ``id`` / ``name`` /
        ``node_type`` / ``status`` / ``version`` / ``run_status``。
    """
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
    """读取单个 Case 详情（只读）。

    Args:
        case_id: Case 主键。

    Returns:
        dict: ``id`` / ``name`` / ``node_type`` / ``status`` / ``version`` /
        ``run_status`` / ``params``（完整参数 JSON，白名单见 documents.md 3.1）。

    Raises:
        ValueError: Case 不存在。
    """
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
    """读取 Suite 的递归拓扑快照（只读）。

    Args:
        suite_id: Suite 主键，作为递归拓扑快照的根节点。

    Returns:
        dict: ``id`` / ``name`` / ``status`` / ``version``，以及 ``topology``
        （``suites.services.build_topology_snapshot`` 输出：子 Suite、Case 集合与出边）。

    Raises:
        ValueError: Suite 不存在。
    """
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
    """列出已注册的事件类型（只读）。

    Args:
        include_system: 是否包含系统内置事件（``EventType``）；False 时仅返回
            ``EventTypeRegistry`` 中注册的自定义类型。

    Returns:
        dict: ``count`` 为本次返回条数；``event_types`` 每项含
        ``name`` / ``scope`` / ``description`` 等注册中心字段。
    """
    from apps.execution.registry import EventRegistry

    items = EventRegistry.list_all(include_system=include_system)
    return {'count': len(items), 'event_types': items}


def list_alerts(
    status: str = '',
    severity: str = '',
    limit: int = 30,
) -> dict[str, Any]:
    """列出最近告警（只读，按 ``-created_at`` 排序）。

    Args:
        status: 状态过滤 ``pending`` / ``acknowledged`` / ``resolved``；空串=全部状态。
        severity: 级别过滤 ``low`` / ``medium`` / ``high`` / ``critical``；空串=全部级别。
        limit: 返回条数上限，收敛到 1~100。

    Returns:
        dict: ``count`` 为本次返回条数；``alerts`` 每项含 ``id`` / ``alert_type`` /
        ``severity`` / ``status`` / ``title`` / ``plan_id`` / ``created_at``（ISO-8601）。
    """
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
    """告警统计（只读，与 REST ``/api/execution/alerts/statistics/`` 同口径）。

    Returns:
        dict: ``overview`` 含 ``total`` / ``pending`` / ``acknowledged`` / ``resolved`` /
        ``high_severity`` / ``critical_severity``；``by_type`` 为
        ``[{alert_type, count}]`` 按数量降序。
    """
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
    """读取当日分时序列（只读，直接查库，不访问外部行情源）。

    Args:
        symbol_code: 标的代码，必须已存在于 ``watchlists.Symbol``。
        limit: 返回分时点上限，收敛到 1~500；超出时保留最近 N 个点。

    Returns:
        dict: ``symbol`` / ``market`` 标的与市场；``timezone`` 市场时区名；
        ``session_status`` 为 ``trading`` / ``lunch_break`` / ``closed`` / ``pre_market``；
        ``pre_close`` 最新点的昨收（字符串或 ``None``）；``count`` 为返回点数；
        ``points`` 为序列化分时点（``ts`` UTC ISO-8601、``local_time`` 市场本地时间、
        ``price`` / ``change`` / ``volume`` / ``amount`` / ``avg_price`` / ``high`` /
        ``low`` / ``open_price`` / ``pre_close``）。

    Raises:
        ValueError: 代码为空或标的未入库。
    """
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
    """列出最近的 SuiteRun 执行实例（只读，按 ``-started_at, -id`` 排序）。

    Args:
        plan_id: 按 Plan 主键过滤；0 表示不过滤。
        symbol: 按标的代码精确过滤（去空格）；空串表示不过滤。
        limit: 返回条数上限，收敛到 1~100。

    Returns:
        dict: ``count`` 为本次返回条数；``runs`` 每项含 ``id`` / ``plan_id`` /
        ``suite_id`` / ``symbol`` / ``status`` / ``started_at`` / ``ended_at``（ISO-8601 或 None）。
    """
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
    """受控写操作：为 Plan + 标的创建 pending SuiteRun（不直接下单）。

    Args:
        plan_id: Plan 主键，必须为已发布（``published``）Plan。
        symbols: 标的代码数组（如 ``['000001', '600000']``）；逐项去空格后各建一个
            pending SuiteRun，空数组直接报错。

    Returns:
        dict: ``plan_id`` 回显；``created_run_ids`` 新建 SuiteRun 主键列表；
        ``count`` 为创建数量。

    Raises:
        PermissionError: 写开关未开启（需 ``MCP_ALLOW_TRIGGER=1`` 或 ``--allow-trigger``）。
        ValueError: ``symbols`` 为空。
        apps.execution.services.ExecutionError: Plan 未发布等生命周期校验失败。
        apps.plans.models.Plan.DoesNotExist: ``plan_id`` 不存在。
    """
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
    """返回 MCP 概览文本（同时用于 ``server.instructions`` 与 ``quant://docs/overview`` 资源）。

    Returns:
        str: 系统定位、核心隐喻（Case→Suite→Plan）、传输方式与写操作边界说明。
    """
    return (
        'Quant Engine：本地优先的量化投研与交易系统。\n'
        'Django 负责数据与 REST API；runner 独立进程负责 Plan 调度与 Suite 事件循环。\n'
        '核心隐喻：Case（原子策略）→ Suite（DAG 编排）→ Plan（调度）→ SuiteRun/Order。\n'
        'MCP 服务默认以 SSE（HTTP）传输暴露标的、K 线、策略元数据、告警与分时等只读视图；\n'
        '触发 Plan 需 MCP_ALLOW_TRIGGER=1（或启动参数 --allow-trigger），且只创建 pending SuiteRun，不会直接下单。\n'
        '创建/编辑/删除 Case、Suite、Plan 的配置写操作默认禁用：需 MCP_ALLOW_MUTATE=1 或 --allow-mutate，'
        '且只改 draft 配置，不发布、不启动、不下单；发布与启停仍走 REST 动作接口。\n'
    )
