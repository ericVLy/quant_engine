"""运行总览（Dashboard）的聚合查询。

设计原则：**把统计下推到 DB**。前端不应为了算「成功率 / 活跃运行 / 未确认委托单」
而拉取最多 500 条列表在浏览器里聚合——那会让口径随 ``page_size`` 上限漂移
（实际统计的是「最近 500 条」而非全量），并产生多次往返。

口径约定：

- **窗口统计**（``window_days``）用于成功率、趋势等**比率型**指标；
- **存量/健康度**类指标（活跃运行、过期执行意向、未确认委托单、未处理告警、资金余额）
  取**全表**事实，与窗口无关——它们回答的是「现在系统健康吗」；
- 金额一律按 ``price × volume`` 聚合（禁止 ``Sum('price')`` 单价之和，见 documents.md）；
- 自然日切分使用**市场时区**（默认 ``Asia/Shanghai``），与分时监控的本地日历一致；
- 资金块只暴露**聚合数值**，不暴露 ``account_id``（N-05 / 规则 §12.8）。
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Avg, Count, F, Max, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

#: 终态运行（计入成功率分母）
SETTLED_RUN_STATUSES = ('completed', 'failed', 'stopped')
#: 未结束的运行状态
ACTIVE_RUN_STATUSES = ('pending', 'running')
#: 计入「已用资金」的委托单状态（与 ``runner.risk.DailyLimitPolicy`` 口径一致）
COUNTED_ORDER_STATUSES = ('pending', 'sent', 'filled')
#: 默认统计窗口（天）
DEFAULT_WINDOW_DAYS = 7
#: 默认趋势天数
DEFAULT_TREND_DAYS = 14
#: 自然日切分时区（与分时监控「本地日历」口径一致）
DEFAULT_TREND_TZ = 'Asia/Shanghai'


def _setting(name, default):
    """读取 settings 覆盖值（缺失或为 ``None`` 时用默认）。"""
    value = getattr(settings, name, None)
    return default if value is None else value


def _tz(name=None):
    return ZoneInfo(name or DEFAULT_TREND_TZ)


def _window_start(now, days, tz):
    """返回「含今天共 ``days`` 个自然日」的起始时刻（aware，UTC 存储口径）。

    按市场时区切分自然日，避免 ``TIME_ZONE='UTC'`` 让日期边界落在北京 08:00。
    """
    local_now = now.astimezone(tz)
    first_day = (local_now - timedelta(days=days - 1)).date()
    return datetime.combine(first_day, dtime.min, tzinfo=tz).astimezone(dt_timezone.utc)


def _pct(part, total):
    """百分比（保留 1 位）；分母为 0 时返回 ``None`` 而非 0，避免误读为「0%」。"""
    if not total:
        return None
    return round(part * 100 / total, 1)


def execution_overview(now, since):
    """执行总览：窗口内运行数 / 状态分布 / 成功率 / 平均耗时 + **全表**活跃运行。"""
    from apps.execution.models import ExecutionLog, SuiteRun

    runs = SuiteRun.objects.all()
    window = runs.filter(created_at__gte=since)
    by_status = dict(window.values_list('status').annotate(n=Count('pk')))
    total = sum(by_status.values())
    settled = sum(by_status.get(status, 0) for status in SETTLED_RUN_STATUSES)
    active = runs.filter(status__in=ACTIVE_RUN_STATUSES)
    avg_duration = ExecutionLog.objects.filter(
        trigger_time__gte=since).aggregate(avg=Avg('duration_ms'))['avg']

    return {
        'window_total': total,
        'by_status': by_status,
        'settled_total': settled,
        'success_rate': _pct(by_status.get('completed', 0), settled),
        'failure_rate': _pct(by_status.get('failed', 0), settled),
        'avg_duration_ms': int(avg_duration) if avg_duration else None,
        'active_runs': active.count(),
        'running_runs': active.filter(status='running').count(),
    }


def intent_health(now, pending_max_age=None):
    """执行意向健康度（P1）：待消费 / 超过有效期的意向。

    与 ``apps.execution.recovery.recover_orphaned_runs`` 的收口条件一致，因此这里
    数到的就是「下次调度器启动会被收口成 ``PENDING_EXPIRED``」的条数。
    """
    from apps.execution.models import SuiteRun

    ttl = (_setting('EXECUTION_PENDING_MAX_AGE_SECONDS', 300)
           if pending_max_age is None else pending_max_age)
    pending = SuiteRun.objects.filter(status='pending')
    return {
        'pending': pending.count(),
        'expired_candidates': pending.filter(
            created_at__lte=now - timedelta(seconds=ttl)).count(),
        'max_age_seconds': ttl,
    }


def order_overview(now, since, unconfirmed_after=None):
    """委托单：窗口内笔数 / 方向 / 成交金额（``price × volume``）+ **全表**状态分布。

    ``unconfirmed`` 与恢复器口径一致（``status='pending'`` 且超过判定时长），对应
    P1-3「可能崩在已提交券商、未回写之间」需要人工对账的单。
    """
    from apps.execution.models import Order

    after = (_setting('UNCONFIRMED_ORDER_SECONDS', 1800)
             if unconfirmed_after is None else unconfirmed_after)
    orders = Order.objects.all()
    window = orders.filter(created_at__gte=since)
    counted = window.filter(status__in=COUNTED_ORDER_STATUSES)

    return {
        'window_total': window.count(),
        'by_status': dict(orders.values_list('status').annotate(n=Count('pk'))),
        'by_direction': dict(window.values_list('direction').annotate(n=Count('pk'))),
        'notional_total': counted.aggregate(total=Sum(F('price') * F('volume')))['total'],
        'notional_direction': dict(counted.values_list('direction')
                                   .annotate(total=Sum(F('price') * F('volume')))),
        'unconfirmed': orders.filter(
            status='pending', created_at__lte=now - timedelta(seconds=after)).count(),
        'unconfirmed_after_seconds': after,
    }


def funds_overview():
    """账户资金聚合（**不含 account_id**，见 N-05）。

    ``allocated_capital`` / ``available_capital`` 是模型上的 property（内部按
    ``Plan.account_id`` 聚合），这里只输出数值。
    """
    from apps.execution.models import AccountFundConfig

    config = AccountFundConfig.objects.first()
    if config is None:
        return {'configured': False}
    return {
        'configured': True,
        'total_capital': config.total_capital,
        'allocated_capital': config.allocated_capital,
        'available_capital': config.available_capital,
        'source': config.source,
        'capital_basis': config.capital_basis,
        'synced_at': config.synced_at,
        'is_stale': config.is_stale,
    }


def alert_overview(since):
    """告警：未处理数（**全表**）+ 窗口内级别/类型分布。"""
    from apps.execution.models import Alert

    pending = Alert.objects.filter(status='pending')
    window = Alert.objects.filter(created_at__gte=since)
    return {
        'open': pending.count(),
        'open_by_severity': dict(pending.values_list('severity').annotate(n=Count('pk'))),
        'window_total': window.count(),
        'window_by_type': dict(window.values_list('alert_type').annotate(n=Count('pk'))),
        'latest_at': Alert.objects.aggregate(m=Max('created_at'))['m'],
    }


def config_overview():
    """配置健康度：已发布 Plan/Suite/Case 数量与 Plan 的 ``run_status`` 分布。

    ``run_status`` 长期停在 ``running`` 的 Plan 正是 P2 归一要治的病，这里直接暴露。
    """
    from apps.cases.models import Case
    from apps.plans.models import Plan
    from apps.suites.models import Suite

    published = Plan.objects.filter(status='published')
    return {
        'plans_published': published.count(),
        'plans_by_run_status': dict(published.values_list('run_status')
                                    .annotate(n=Count('pk'))),
        'suites_published': Suite.objects.filter(status='published').count(),
        'cases_published': Case.objects.filter(status='published').count(),
    }


def data_freshness_overview():
    """数据新鲜度：分时最后采样时间 + 标的/分组规模。

    分时数据是当日临时数据，``None`` 表示尚未采样（收盘后被清空属正常）。
    """
    from apps.monitoring.models import IntradayPoint
    from apps.watchlists.models import Group, Symbol

    return {
        'intraday_last_at': IntradayPoint.objects.aggregate(m=Max('ts'))['m'],
        'intraday_points': IntradayPoint.objects.count(),
        'symbols': Symbol.objects.count(),
        'groups': Group.objects.count(),
    }


def overview(window_days=DEFAULT_WINDOW_DAYS, pending_max_age=None,
             unconfirmed_after=None, trend_tz=None, now=None):
    """运行总览快照：一次请求拿到全部 KPI。

    单对象接口，**不分页**（N-01：单对象/详情/POST 动作保持原结构）。
    """
    now = now or timezone.now()
    window_days = max(1, int(window_days or DEFAULT_WINDOW_DAYS))
    tz = _tz(trend_tz)
    since = _window_start(now, window_days, tz)

    return {
        'generated_at': now,
        'window_days': window_days,
        'window_start': since,
        'timezone': str(tz),
        'execution': execution_overview(now, since),
        'intents': intent_health(now, pending_max_age),
        'orders': order_overview(now, since, unconfirmed_after),
        'funds': funds_overview(),
        'alerts': alert_overview(since),
        'config': config_overview(),
        'data_freshness': data_freshness_overview(),
    }


def execution_trend(days=DEFAULT_TREND_DAYS, trend_tz=None, now=None):
    """按自然日（市场时区）返回执行趋势：每天总运行数 / 各终态数 / 成功率。

    缺失日期补 0，保证前端图表 X 轴连续（否则「当天没跑」的日期会从图上消失，
    与「跑了 0 次」无法区分）。
    """
    now = now or timezone.now()
    days = max(1, int(days or DEFAULT_TREND_DAYS))
    tz = _tz(trend_tz)
    since = _window_start(now, days, tz)

    from apps.execution.models import SuiteRun

    rows = (SuiteRun.objects.filter(created_at__gte=since)
            .annotate(day=TruncDate('created_at', tzinfo=tz))
            .values('day', 'status')
            .annotate(n=Count('pk')))
    bucket = {}
    for row in rows:
        # 键统一成 ISO 字符串：DB 返回的是 date 对象，与下面按 ISO 串查表才能对上
        day_key = row['day'].isoformat() if hasattr(row['day'], 'isoformat') else str(row['day'])
        bucket.setdefault(day_key, {})[row['status']] = row['n']

    last_day = now.astimezone(tz).date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = (last_day - timedelta(days=offset)).isoformat()
        counts = bucket.get(day, {})
        settled = sum(counts.get(status, 0) for status in SETTLED_RUN_STATUSES)
        series.append({
            'date': day,
            'total': sum(counts.values()),
            'completed': counts.get('completed', 0),
            'failed': counts.get('failed', 0),
            'stopped': counts.get('stopped', 0),
            'settled': settled,
            'success_rate': _pct(counts.get('completed', 0), settled),
        })
    return series
