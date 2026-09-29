"""把 ``Plan.run_status`` 与执行事实（``SuiteRun``）对齐（P2 状态归一）。

背景：``Plan/Suite/Case.run_status``（编排会话状态）与 ``SuiteRun`` / ``NodeRun`` /
``ExecutionLog``（执行真相）是两套状态源。``run_status`` 原本只由 REST 手动
``/start`` ``/stop`` 动作流转，而自动收口链条
``complete_case → _try_complete_suite → _try_complete_plan`` 在生产链路**零调用者**
（``runner`` 从不调用这些函数），导致：

- 调度器驱动的执行永远不会把 Plan 从 ``running`` 置为 ``done``；
- Plan 长时间停在 ``running``，与真实执行事实分叉（实测长期存在）。

本模块按 ``SuiteRun`` 事实做**数据驱动**归一，不依赖内存中的事件链：

============================  =========================================
``SuiteRun`` 现状              归一后的 ``Plan.run_status``
============================  =========================================
无任何运行                      不变（保持 ``new``，不臆断）
存在 ``pending`` / ``running``   ``running``
全部终态且全部 ``completed``     ``done``
全部终态且含 ``failed``/``stopped``  ``interrupt``
============================  =========================================

范围说明：只归一 **Plan**。``Suite`` / ``Case.run_status`` 是**每个实体单槽**，而执行
按 ``(Plan, 标的)`` 多路并发，单槽无法表达并发事实，自动归一会产生误导，故仍只由
REST 手动 ``/start`` ``/stop`` 流转（见 README「已知边界与限制」）。
"""
from __future__ import annotations

import logging

from django.db.models import Count

logger = logging.getLogger(__name__)

#: 视为「未结束」的运行状态
ACTIVE_RUN_STATUSES = ('pending', 'running')


def plan_run_status_from_runs(plan):
    """按执行事实推导 ``Plan.run_status``；**无运行记录时返回 ``None``**（不臆断）。

    Args:
        plan: ``Plan`` 实例或主键。

    Returns:
        str | None: ``'running'`` / ``'done'`` / ``'interrupt'``；无运行记录时 ``None``。
    """
    from apps.execution.models import SuiteRun

    plan_id = getattr(plan, 'pk', plan)
    counts = dict(
        SuiteRun.objects.filter(plan_id=plan_id)
        .values_list('status')
        .annotate(total=Count('pk'))
    )
    if not counts:
        return None
    if any(counts.get(status, 0) for status in ACTIVE_RUN_STATUSES):
        return 'running'
    if counts.get('completed', 0) == sum(counts.values()):
        return 'done'
    return 'interrupt'


def reconcile_plan_run_status(plan):
    """把单个 Plan 的 ``run_status`` 归一到执行事实（幂等）。

    已是目标态时**不写库**（0 变更），因此可在每次执行结束后安全调用。

    Args:
        plan: ``Plan`` 实例（使用其 ``pk`` 与 ``run_status``）。

    Returns:
        dict: ``{'plan_id', 'from', 'to', 'changed'}``。
    """
    target = plan_run_status_from_runs(plan)
    previous = plan.run_status
    if target is None or target == previous:
        return {'plan_id': plan.pk, 'from': previous, 'to': previous, 'changed': False}

    plan.run_status = target
    plan.save(update_fields=['run_status', 'updated_at'])
    logger.info('[state-sync] Plan %s 的 run_status 按执行事实归一：%s → %s',
                plan.pk, previous, target)
    return {'plan_id': plan.pk, 'from': previous, 'to': target, 'changed': True}


def reconcile_all_plan_run_statuses(plan_ids=None, only_active=True):
    """批量归一（调度器启动期用），修复历史遗留的错位状态。

    Args:
        plan_ids: 指定 Plan 主键；``None`` 表示扫描。
        only_active: 未指定 ``plan_ids`` 时只处理 ``run_status='running'`` 的 Plan
            （避免对海量 ``new`` / ``done`` 计划做无谓查询与写库）。

    Returns:
        dict: ``{'checked': int, 'changed': list[dict], 'changed_count': int}``。
    """
    from apps.plans.models import Plan

    queryset = Plan.objects.all()
    if plan_ids is not None:
        queryset = queryset.filter(pk__in=list(plan_ids))
    elif only_active:
        queryset = queryset.filter(run_status='running')

    changed = []
    checked = 0
    for plan in queryset.iterator():
        checked += 1
        result = reconcile_plan_run_status(plan)
        if result['changed']:
            changed.append(result)
    if changed:
        logger.warning('[state-sync] 启动期归一 Plan run_status %s 个：%s',
                       len(changed), [(c['plan_id'], f"{c['from']}→{c['to']}")
                                      for c in changed])
    return {'checked': checked, 'changed': changed, 'changed_count': len(changed)}
