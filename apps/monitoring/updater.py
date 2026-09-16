"""分时数据源内部更新器（随 Django 服务启动）。

设计（见 documents.md 模块9）：
- Django 进程启动时（AppConfig.ready）拉起后台守护线程，自主管理分时数据更新；
- **禁止单独的更新命令**：``sample_intraday`` 管理命令已移除，
  采样 / 启动清理 / 启动回填 / 开盘清理 / 收盘清理全部由本更新器在进程内完成；
- 循环职责：
  1. **启动时清空分时数据**（每进程一次；随后由启动回填重建当日数据）；
  2. 启动时执行 ``backfill_intraday``（开盘到当前的完整性回填）；
  3. 每 ``MONITORING_UPDATER_INTERVAL`` 秒执行一轮 ``sample_intraday``；
  4. **市场开盘时清理历史数据**（每市场每本地日一次：删除 ts 早于当日当地零点的记录）；
  5. UTC 23:00 触发 ``clear_intraday`` 收盘兜底清理（每自然日最多一次，幂等）。
"""
import logging
import threading
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

from .market_calendar import MARKET_TIMEZONES, in_trading_session, to_market_local
from .services import backfill_intraday, clear_intraday, sample_intraday

logger = logging.getLogger(__name__)

# UTC 23:00 收盘清理触发小时（美股收盘后、A股开盘前）
CLEANUP_UTC_HOUR = 23


class IntradayUpdater:
    """进程内分时数据更新器；``run_once`` 可脱离线程单独测试。"""

    def __init__(self, interval=None):
        self.interval = interval or getattr(settings, 'MONITORING_UPDATER_INTERVAL', 60)
        self._stop_event = threading.Event()
        self._thread = None
        self._backfill_complete = False
        self._last_cleanup_date = None
        self._startup_cleared = False
        self._market_clear_dates = {}  # market -> 已执行开盘清理的本地日期

    # ---- 生命周期 ------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(target=self._loop, name='monitoring-updater', daemon=True)
        self._thread.start()
        logger.info('[monitoring-updater] 已启动，采样间隔 %ss', self.interval)
        return self._thread

    def stop(self, timeout=None):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self._thread

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # ---- 主循环 --------------------------------------------------------
    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._startup_clear()
                self._maybe_backfill()
                self.run_once()
                self._maybe_open_clear()
                self._maybe_cleanup()
            except Exception:  # pylint: disable=broad-except
                logger.exception('[monitoring-updater] 单轮更新异常，下一轮重试')
            self._stop_event.wait(self.interval)

    def run_once(self, provider=None):
        """执行一轮采样（供主循环与测试复用）。"""
        summary = sample_intraday(provider=provider)
        for market, info in sorted(summary.items()):
            logger.info(
                '[monitoring-updater] [%s] sampled=%s skipped=%s failed=%s',
                market, info['sampled'], info['skipped'], len(info['failed']),
            )
        return summary

    def _startup_clear(self, now=None):
        """服务启动时清空全部分时数据（每进程一次，幂等）。

        清空后由 ``_maybe_backfill`` 重建当日数据（gm 逐分钟历史可回填）；
        避免上次运行 / 前一交易日遗留的分时点与新会话混叠。
        """
        if self._startup_cleared:
            return
        now = now if now is not None else timezone.now()
        try:
            # ts <= now 全部清除（分钟对齐的点可能恰好等于 now，边界 +1s）
            deleted = clear_intraday(before=now + timedelta(seconds=1))
            self._startup_cleared = True
            logger.info('[monitoring-updater] 启动清空分时数据，删除 %s 条', deleted)
        except Exception:  # pylint: disable=broad-except
            # 清理失败不置位，下一轮重试（未清空前不开始采样/回填会导致混叠，宁可重试）
            logger.exception('[monitoring-updater] 启动清空异常，下一轮重试')

    def _maybe_open_clear(self, now=None):
        """市场开盘时清理历史数据（每市场每本地日一次）。

        当某市场处于交易时段且其本地日期尚未清理过时，删除 ``ts`` 早于
        当日当地 00:00 的全部记录（即往日历史数据），当日数据保留。
        非交易时段 / 周末不触发（UTC 23:00 收盘清理兜底）。
        """
        now = now if now is not None else timezone.now()
        for market in MARKET_TIMEZONES:
            local_now = to_market_local(now, market)
            local_date = local_now.date()
            if self._market_clear_dates.get(market) == local_date:
                continue
            if not in_trading_session(market, local_now):
                continue
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            deleted = clear_intraday(before=local_midnight.astimezone(dt_timezone.utc))
            self._market_clear_dates[market] = local_date
            logger.info(
                '[monitoring-updater] [%s] 开盘清理历史数据（< %s），删除 %s 条',
                market, local_midnight.strftime('%Y-%m-%d %H:%M %Z'), deleted,
            )

    def _maybe_backfill(self):
        """启动完整性回填；**不完整时每轮重试**直到完整（覆盖启动瞬时故障，
        如 gm 终端连接未就绪）。完整后跳过，避免每轮全表比对。"""
        if self._backfill_complete:
            return
        try:
            summary = backfill_intraday()
        except Exception:  # pylint: disable=broad-except
            logger.exception('[monitoring-updater] 启动回填异常，下一轮重试')
            return
        incomplete = {
            market: info['missing_remaining']
            for market, info in summary.items()
            if info['missing_remaining']
        }
        for market, info in sorted(summary.items()):
            logger.info(
                '[monitoring-updater] 回填 [%s] checked=%s complete=%s backfilled=%s pending=%s',
                market, info['checked'], info['complete'], info['backfilled'],
                len(info['missing_remaining']),
            )
        if not incomplete:
            self._backfill_complete = True
        elif not getattr(self, '_backfill_warned', False):
            # 首次发现不完整即告警一次，避免每轮刷屏
            logger.warning('[monitoring-updater] 回填不完整，下一轮继续重试: %s', incomplete)
            self._backfill_warned = True


    def _maybe_cleanup(self, now=None):
        now = now if now is not None else timezone.now()
        today = now.date()
        if now.hour >= CLEANUP_UTC_HOUR and self._last_cleanup_date != today:
            deleted = clear_intraday(now=now)
            self._last_cleanup_date = today
            logger.info('[monitoring-updater] UTC23:00 收盘清理，删除 %s 条', deleted)


_instance: IntradayUpdater | None = None
_instance_lock = threading.Lock()


def get_updater():
    """进程级单例（AppConfig.ready 与其他入口共享同一个更新器）。"""
    global _instance  # pylint: disable=global-statement
    with _instance_lock:
        if _instance is None:
            _instance = IntradayUpdater()
        return _instance
