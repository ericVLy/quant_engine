"""执行日志生命周期管理（N-04：执行日志保留 30 天，自动清理 + 归档策略）。

清理范围（严格遵守验收标准「清理不影响未完成运行和订单」）：

| 对象 | 处理 | 原因 |
|------|------|------|
| `Event` | ✅ 删除（所属 SuiteRun 为终态且过期） | 纯事件痕迹，无订单关联 |
| `NodeRun` | ✅ 删除（同上） | 节点执行轨迹（回放数据） |
| `ExecutionLog` | ✅ 删除（过期且**无关联订单**） | 日志主体 |
| `ExecutionLog`（含订单） | ⛔ 保留 | `Order.log` 为 `on_delete=CASCADE`，删日志会连带删交易明细 |
| `Order` | ⛔ 始终保留 | 交易明细属财务留档 |
| `SuiteRun` | ⛔ 始终保留 | 体量小，是 Order/Alert 的关联锚点与统计口径 |
| `Alert` / `FundAllocation` | ⛔ 始终保留 | 告警处理痕迹与资金占用记录 |

- 仅处理**终态** SuiteRun（`completed` / `failed` / `stopped`）；`pending` / `running` 一律不碰；
- 幂等：重复执行第二次删除 0 条；
- 支持 `dry_run`：只统计不删除。
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import Event, ExecutionLog, NodeRun, SuiteRun

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = ('completed', 'failed', 'stopped')
DEFAULT_RETENTION_DAYS = 30


class RetentionError(ValueError):
    """清理参数非法（如负数天数）。"""


def retention_days():
    """读取保留天数（settings 可配，默认 30 天）。"""
    return int(getattr(settings, 'EXECUTION_LOG_RETENTION_DAYS', DEFAULT_RETENTION_DAYS))


def purge_execution_history(days=None, now=None, dry_run=False):
    """清理过期执行痕迹，返回统计字典。

    ``cutoff`` 取 ``now - days``；终态运行按结束时间判定（缺失时回退创建时间），
    ``ExecutionLog`` 按 ``trigger_time`` 判定，且仅删除没有关联订单的记录。
    """
    days = retention_days() if days is None else int(days)
    if days < 0:
        raise RetentionError('保留天数不能为负数')
    now = now if now is not None else timezone.now()
    cutoff = now - timedelta(days=days)

    stale_runs = SuiteRun.objects.filter(
        status__in=TERMINAL_RUN_STATUSES,
    ).filter(
        Q(ended_at__lt=cutoff) | Q(ended_at__isnull=True, created_at__lt=cutoff),
    )
    stale_run_ids = list(stale_runs.values_list('id', flat=True))

    events = Event.objects.filter(run_id__in=stale_run_ids)
    node_runs = NodeRun.objects.filter(run_id__in=stale_run_ids)
    # 有订单的日志必须保留（Order.log 为 CASCADE，删除会连带删交易明细）
    # ExecutionLog 无 created_at 字段，trigger_time 为服务端自动写入，直接作为过期判定
    stale_logs = ExecutionLog.objects.filter(trigger_time__lt=cutoff)
    purgable_logs = stale_logs.filter(orders__isnull=True).distinct()
    guarded_logs = stale_logs.filter(orders__isnull=False).distinct()

    stats = {
        'cutoff': cutoff.isoformat(),
        'days': days,
        'dry_run': bool(dry_run),
        'runs_scanned': len(stale_run_ids),
        'events': events.count(),
        'node_runs': node_runs.count(),
        'logs': purgable_logs.count(),
        'logs_kept_with_orders': guarded_logs.count(),
    }
    if dry_run:
        return stats

    stats['events'] = _delete(events)
    stats['node_runs'] = _delete(node_runs)
    stats['logs'] = _delete(purgable_logs)
    logger.info(
        '[retention] 执行痕迹清理完成：runs=%s events=%s node_runs=%s logs=%s '
        '（保留含订单日志 %s 条） cutoff=%s',
        stats['runs_scanned'], stats['events'], stats['node_runs'],
        stats['logs'], stats['logs_kept_with_orders'], stats['cutoff'],
    )
    return stats


def _delete(queryset):
    deleted, _ = queryset.delete()
    return deleted