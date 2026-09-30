"""非正常终止运行的收口恢复（P0 重启恢复）。

背景：runner 的执行痕迹（``SuiteRun`` / ``Event`` / ``NodeRun``）本身是持久化的，
但此前**没有任何恢复机制**——进程被 ``kill -9`` / OOM / 断电后，在途运行会永久停在
``running``，且既不告警、也永不被清理：

- ``SuiteRun.status='running'``：事件循环中途被杀，``event_queue`` 非空、
  部分 ``Event.status='processing'``、``NodeRun(status='running')`` 未收口；
- ``apps/execution/retention.py`` 只处理终态（``completed``/``failed``/``stopped``），
  因此这类运行永不被清理，会持续污染 ``/api/execution/runs/`` 与 MCP ``list_suite_runs``；
- 崩溃发生在进程内 ``except`` 之外，``PlanExecutionService._alert_failure``
  拿不到异常，所以**失败是静默的**。

本模块提供启动期**幂等**收口：把上次进程遗留的 ``running`` 运行标记为失败、
回退在途事件、收口未完成节点，并补发 ``suite_failed`` 告警。

设计约束：

- **只收口，不自动续跑**：续跑需要节点级幂等（``EventLoop._fired_edges`` 等状态
  在内存中，重放会重复命中边 → 重复下单），属后续增强（P1）；
- **只处理 ``running``**：``pending`` 可能是刚创建的合法执行意向（MCP
  ``trigger_plan_execution``），不做默认收口，仅在统计中上报；
- **幂等**：行锁 + 事务 + 状态复检，重复执行为 0 变更；
- 告警失败不回滚收口；收口本身的异常由调用方决定是否阻断启动。
"""
# pylint: disable=too-many-positional-arguments  # 执行/构造依赖以位置参数注入（编辑与执行分离），参数列表本身即契约
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Event, ExecutionLog, NodeRun, Order, SuiteRun

logger = logging.getLogger(__name__)

#: 「未确认委托单」的判定时长（秒）：``pending`` 超过该时长仍无外部订单号，
#: 说明进程可能崩在「已提交券商、未回写」之间，本地无法判定，只能提示对账。
UNCONFIRMED_ORDER_AFTER = 1800

#: 收口原因码（便于运维按 code 检索「上次进程遗留」的运行）
ORPHAN_ERROR_CODE = 'ORPHANED_BY_RESTART'
ORPHAN_ERROR_MESSAGE = (
    '进程重启：上次运行的进程非正常终止（kill/崩溃/断电），在途运行已自动收口为失败；'
    '未自动续跑，请核对本次运行的事件与节点轨迹后手工重跑。'
)

#: 执行意向过期原因码（``pending`` 运行在有效期内未被调度器消费）
PENDING_EXPIRED_CODE = 'PENDING_EXPIRED'
PENDING_EXPIRED_MESSAGE = (
    '执行意向超时：pending 运行在有效期内未被调度器消费，已自动收口为失败；'
    '如需执行请重新触发（MCP 受控触发或 REST 触发接口）。'
)


def recover_orphaned_runs(now=None, run_ids=None, dry_run=False, notify=True,
                          pending_max_age=None, unconfirmed_order_after=None):
    """收口上次进程遗留的 ``running`` 运行，并可选收口过期的 ``pending`` 执行意向。

    Args:
        now: 收口时间戳（默认 ``timezone.now()``）；测试可注入。
        run_ids: 只处理指定主键；``None`` 表示扫描全部候选运行。
        dry_run: 只统计不写库。
        notify: 是否为每个被收口的运行补发 ``suite_failed`` 告警。
        pending_max_age: ``pending`` 执行意向的有效期（秒）。给定时，超出该时长仍
            未被调度器消费的意向收口为 ``PENDING_EXPIRED``（避免陈旧交易意图在
            数小时后被补投执行）；``None``（默认）表示不触碰 ``pending``。
        unconfirmed_order_after: 「超时未确认委托单」的判定时长（秒），缺省取
            :data:`UNCONFIRMED_ORDER_AFTER`（1800 秒）。这类单本地无法判定是否
            已到达券商（gm 下单接口无幂等键），只统计上报以便对账。

    Returns:
        dict: ``runs`` / ``run_ids`` / ``events_reset`` / ``node_runs_failed`` /
        ``logs`` / ``alerts`` / ``pending_expired`` / ``pending_expired_ids`` /
        ``pending_runs``（收口后仍存在的 pending 数量）/ ``unconfirmed_orders`` /
        ``unconfirmed_order_ids`` / ``dry_run`` / ``now``。

    Raises:
        Exception: 数据库写失败时原样抛出（调用方决定是否阻断启动）。
    """
    now = now if now is not None else timezone.now()
    orphans = SuiteRun.objects.filter(status='running')
    if run_ids is not None:
        orphans = orphans.filter(pk__in=list(run_ids))
    orphan_ids = list(orphans.order_by('pk').values_list('pk', flat=True))

    expired_ids = []
    if pending_max_age is not None:
        expired = SuiteRun.objects.filter(
            status='pending', created_at__lte=now - timedelta(seconds=pending_max_age))
        if run_ids is not None:
            expired = expired.filter(pk__in=list(run_ids))
        expired_ids = list(expired.order_by('pk').values_list('pk', flat=True))

    # 「已提交券商但未回写」的委托单本地无法判定（gm 下单接口无 cl_ord_id 幂等键），
    # 只能统计并提示人工/对账任务处理，避免它随「收口」被静默忽略。
    stale_after = (UNCONFIRMED_ORDER_AFTER if unconfirmed_order_after is None
                   else unconfirmed_order_after)
    unconfirmed = list(Order.objects
                       .filter(status='pending',
                               created_at__lte=now - timedelta(seconds=stale_after))
                       .order_by('pk').values_list('pk', flat=True)[:50])

    stats = {
        'now': now.isoformat(),
        'dry_run': bool(dry_run),
        'runs': len(orphan_ids),
        'run_ids': orphan_ids,
        'events_reset': 0,
        'node_runs_failed': 0,
        'logs': 0,
        'alerts': 0,
        'pending_expired': len(expired_ids),
        'pending_expired_ids': expired_ids,
        'pending_runs': 0,
        'unconfirmed_orders': len(unconfirmed),
        'unconfirmed_order_ids': unconfirmed,
    }
    if dry_run:
        stats['pending_runs'] = SuiteRun.objects.filter(status='pending').count()
        return stats

    recovered, expired_done = [], []
    for run_id in orphan_ids:
        item = _recover_one(run_id, now=now, expected_status='running',
                            error_code=ORPHAN_ERROR_CODE,
                            error_message=ORPHAN_ERROR_MESSAGE, notify=notify)
        if item is None:
            continue  # 已被并发实例收口
        recovered.append(run_id)
        _accumulate(stats, item)

    for run_id in expired_ids:
        item = _recover_one(run_id, now=now, expected_status='pending',
                            error_code=PENDING_EXPIRED_CODE,
                            error_message=PENDING_EXPIRED_MESSAGE, notify=notify)
        if item is None:
            continue  # 已被调度器消费（认领）或已被其他实例收口
        expired_done.append(run_id)
        _accumulate(stats, item)

    stats['runs'] = len(recovered)
    stats['run_ids'] = recovered
    stats['pending_expired'] = len(expired_done)
    stats['pending_expired_ids'] = expired_done
    stats['pending_runs'] = SuiteRun.objects.filter(status='pending').count()

    if recovered:
        logger.warning(
            '[recovery] 已收口 %s 个上次进程遗留的未完成运行：%s'
            '（事件回退 %s，节点收口 %s，告警 %s）',
            len(recovered), recovered, stats['events_reset'],
            stats['node_runs_failed'], stats['alerts'],
        )
    if expired_done:
        logger.warning(
            '[recovery] 已收口 %s 个过期的 pending 执行意向（有效期 %s 秒）：%s',
            len(expired_done), pending_max_age, expired_done,
        )
    if stats['pending_runs']:
        logger.warning(
            '[recovery] 仍有 %s 个 pending 运行待调度器消费', stats['pending_runs'],
        )
    if unconfirmed:
        logger.warning(
            '[recovery] 发现 %s 张超时未确认的委托单（status=pending 且无外部订单号，'
            '可能崩在「已提交券商、未回写」之间），需与券商对账：%s',
            len(unconfirmed), unconfirmed,
        )
    return stats


def _recover_one(run_id, now, expected_status, error_code, error_message, notify=True):
    """在行锁事务内收口单个运行；状态已不是 ``expected_status`` 时返回 ``None``。"""
    with transaction.atomic():
        run = (SuiteRun.objects.select_for_update()
               .select_related('plan', 'suite')
               .filter(pk=run_id).first())
        if run is None or run.status != expected_status:
            return None  # 已被并发实例收口，或已被消费

        events_reset = 0
        node_runs_failed = 0
        if expected_status == 'running':
            # 在途事件回退为 pending：保留 event_queue 与事件痕迹，便于人工核对/重跑
            events_reset = Event.objects.filter(run=run, status='processing').update(
                status='pending', processed_at=None,
            )
            node_runs_failed = NodeRun.objects.filter(run=run, status='running').update(
                status='failed', ended_at=now,
            )
        run.status = 'failed'
        run.ended_at = now
        run.save(update_fields=['status', 'ended_at'])

        item = {
            'events_reset': events_reset,
            'node_runs_failed': node_runs_failed,
            'logs': _mark_log_failed(run, error_code, error_message),
            'alerts': 0,
        }

    # 事务外补发告警：告警失败不回滚已完成的收口
    if notify:
        item['alerts'] = _alert(run, error_code, error_message)
    return item


def _mark_log_failed(run, error_code, error_message):
    """把运行的执行日志标记为失败；缺失时补建（已存在的轨迹字段不覆盖）。"""
    task_id = f'suite-run-{run.pk}'
    log = ExecutionLog.objects.filter(task_id=task_id).first()
    if log is None:
        ExecutionLog.objects.create(
            plan=run.plan, symbol=run.symbol, task_id=task_id,
            final_direction=0, status='failed',
            error_msg=error_message, error_code=error_code,
        )
        return 1
    log.status = 'failed'
    log.error_msg = error_message
    log.error_code = error_code
    log.save(update_fields=['status', 'error_msg', 'error_code'])
    return 1


def _alert(run, error_code, error_message):
    """补发 ``suite_failed`` 告警；失败只记日志，返回 0/1。"""
    try:
        from .alerts import alert_service

        alert_service.create_suite_failed_alert(
            suite_run=run, error_message=error_message, error_code=error_code,
        )
        return 1
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning('[recovery] 补发失败告警出错（不影响收口）：%s', exc)
        return 0


def _accumulate(stats, item):
    """把单次收口的结果累加进统计字典。"""
    stats['events_reset'] += item['events_reset']
    stats['node_runs_failed'] += item['node_runs_failed']
    stats['logs'] += item['logs']
    stats['alerts'] += item['alerts']
