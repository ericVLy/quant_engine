"""分时数据源内部更新器（随 Django 服务启动）。

设计（见 documents.md 模块9）：
- Django 进程启动时（AppConfig.ready）拉起后台守护线程，自主管理分时数据更新；
- **禁止单独的更新命令**：``sample_intraday`` 管理命令已移除，
  采样 / 启动回填 / 收盘清理全部由本更新器在进程内完成；
- 循环职责：
  1. 启动时执行一次 ``backfill_intraday``（开盘到当前的完整性回填）；
  2. 每 ``MONITORING_UPDATER_INTERVAL`` 秒执行一轮 ``sample_intraday``；
  3. UTC 23:00 触发当日 ``clear_intraday``（每自然日最多一次，幂等）。
"""
import logging
import threading

from django.conf import settings
from django.utils import timezone

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
                self._maybe_backfill()
                self.run_once()
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
