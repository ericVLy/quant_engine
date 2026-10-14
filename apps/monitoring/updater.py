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
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .market_calendar import MARKET_TIMEZONES, in_trading_session, market_timezone, to_market_local
from .services import backfill_intraday, clear_intraday, sample_intraday

logger = logging.getLogger(__name__)

# UTC 23:00 收盘清理触发小时（美股收盘后、A股开盘前）
CLEANUP_UTC_HOUR = 23

#: 子进程心跳文件（父进程据此判断采样进程是否卡死）
DEFAULT_HEARTBEAT_PATH = Path(tempfile.gettempdir()) / 'quant-intraday-updater.heartbeat'


class IntradayUpdater:
    """进程内分时数据更新器；``run_once`` 可脱离线程单独测试。"""

    def __init__(self, interval=None, heartbeat_path=None):
        self.interval = interval or getattr(settings, 'MONITORING_UPDATER_INTERVAL', 60)
        # 每轮循环触碰该文件，供父进程判断子进程是否卡死
        self.heartbeat_path = heartbeat_path or DEFAULT_HEARTBEAT_PATH
        self._stop_event = threading.Event()
        self._thread = None
        self._backfill_complete = False
        self._backfill_warned = False
        self._last_cleanup_date = None
        self._startup_cleared = False
        self._market_clear_dates = {}  # market -> 已执行开盘清理的本地日期

    # ---- 生命周期 ------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        # 清停止标志：否则 stop() 之后无法再次启动（守护/测试会复用同一实例）
        self._stop_event.clear()
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
            # 心跳放在 try 之外：即使本轮抛异常也要刷新，
            # 否则父进程会误判子进程卡死并反复重启它。
            self._touch_heartbeat()
            self._stop_event.wait(self.interval)

    def _touch_heartbeat(self):
        """写心跳文件（mtime 即最近一轮完成时间）。失败不影响采样。"""
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.touch()
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.debug('[monitoring-updater] 心跳写入失败: %s', exc)

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
            # naive 本地零点必须先挂市场时区再转 UTC；直接 astimezone 会被
            # 解释为系统时区（Windows +08 碰巧正确，Linux UTC 下边界错 8 小时）。
            midnight_utc = local_midnight.replace(tzinfo=market_timezone(market))
            deleted = clear_intraday(before=midnight_utc.astimezone(dt_timezone.utc))
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
        elif not self._backfill_warned:
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


_instance: IntradayUpdater | None = None  # pylint: disable=invalid-name  # 可变的进程级单例句柄，非模块常量
_instance_lock = threading.Lock()


def get_updater():
    """进程级单例（AppConfig.ready 与其他入口共享同一个更新器）。"""
    global _instance  # pylint: disable=global-statement
    with _instance_lock:
        if _instance is None:
            _instance = IntradayUpdater()
        return _instance


class SubprocessIntradayUpdater:
    """以**独立子进程**运行分时更新器；父进程只做守护（拉起 / 重启 / 看门狗）。

    为什么要子进程（2026-10-14）：
    分时采样经``GmSnapshotProvider`` 调用 gm SDK的 ``api.history``。实测该调用
    在对端"可连接但不响应"时**持有 GIL 一直阻塞**——同进程内的心跳线程与
    ``asyncio`` 事件循环全部停摆，``wait_for`` / ``join(timeout)`` 的超时逻辑
    根本执行不到，**整个 Web 进程被冻结**（浏览器一直转圈、整站打不开）。
    （对照：终端真正关闭时 gm 会~2s 抛``GmError:无法连接到终端服务``，不会卡死。）

    进程隔离后，卡死只影响子进程：父进程可 ``kill()`` 后重启，Web 服务毫发无损。

    看门狗：子进程每轮循环触碰心跳文件；超过 ``max_staleness`` 秒未更新即视为卡死，
    父进程强制杀掉并重启（应对"子进程存活但卡在 gm 调用里"）。
    """

    def __init__(self, interval=None, max_staleness=None, base_dir=None):
        self.interval = interval or getattr(settings, 'MONITORING_UPDATER_INTERVAL', 60)
        # 心跳超时阈值：留足 3 轮余量，容忍 gc/IO 抖动
        self.max_staleness = max_staleness or max(self.interval * 3, 180)
        self.base_dir = base_dir or Path(__file__).resolve().parents[2]
        self.heartbeat_path = DEFAULT_HEARTBEAT_PATH
        self._stop_event = threading.Event()
        self._thread = None
        self._proc = None
        self._lock = threading.Lock()

    # ---- 生命周期 ------------------------------------------------------
    def start(self):
        """启动守护线程（不是采样本身）。"""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervise, name='monitoring-updater-supervisor', daemon=True)
        self._thread.start()
        logger.info('[monitoring-updater] 守护线程已启动（采样在子进程中运行，间隔 %ss）',
                    self.interval)
        return self._thread

    def stop(self, timeout=5):
        """停止守护线程并终止子进程。"""
        self._stop_event.set()
        self._kill_child()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self._thread

    @property
    def is_running(self):
        """采样子进程是否存活。"""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    # ---- 内部 ----------------------------------------------------------
    def _spawn(self):
        env = os.environ.copy()
        # 子进程自己就是采样进程，必须关掉内置更新器，否则会递归再拉一个子进程
        env['MONITORING_UPDATER_ENABLED'] = '0'
        env.setdefault('DJANGO_SETTINGS_MODULE', 'quant_engine.settings.dev')
        cmd = [sys.executable, str(self.base_dir / 'manage.py'), 'run_intraday_updater']
        logger.info('[monitoring-updater] 拉起采样子进程: %s', ' '.join(cmd))
        with self._lock:
            # 刻意不用 `with`：子进程需跨多次 _supervise_once 存活，
            # 生命周期由 _kill_child / 看门狗显式管理（不能随 spawn 语句结束就关闭）。
            # pylint: disable=consider-using-with
            self._proc = subprocess.Popen(  # noqa: S603 # 固定参数，无 shell
                cmd, cwd=str(self.base_dir), env=env)

    def _kill_child(self, reason=''):
        """终止子进程（SIGKILL，由操作系统处理，不依赖目标进程响应）。"""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        logger.warning('[monitoring-updater] 终止采样子进程%s (pid=%s)',
                       f'（{reason}）' if reason else '', proc.pid)
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # pylint: disable=broad-except
            logger.exception('[monitoring-updater] 终止采样子进程异常')

    def _heartbeat_age(self):
        """心跳文件距今秒数；不存在返回 ``None``。"""
        try:
            return time.time() - self.heartbeat_path.stat().st_mtime
        except OSError:
            return None

    def _supervise(self):
        while not self._stop_event.is_set():
            try:
                self._supervise_once()
            except Exception:  # pylint: disable=broad-except
                logger.exception('[monitoring-updater] 守护循环异常，下一轮重试')
            self._stop_event.wait(min(self.interval, 15))

    def _supervise_once(self):
        if not self.is_running:
            if self._proc is not None:
                code = self._proc.poll()
                logger.warning('[monitoring-updater] 采样子进程已退出（rc=%s），重启', code)
            self._spawn()
            return
        age = self._heartbeat_age()
        if age is not None and age > self.max_staleness:
            # 子进程存活但心跳停滞 → 很可能卡在 gm 调用里（持有 GIL，进程无法响应）
            self._kill_child(reason=f'心跳停滞 {age:.0f}s（疑似卡在 gm 调用）')
