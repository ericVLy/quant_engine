"""TaskQueue 的生产端执行服务：把 ``(Plan, Symbol)`` 任务真正跑到 SuiteRun / 委托单。

背景（本次修复的问题）：``Scheduler`` 负责把到期的 ``(Plan, Symbol)`` 投递到
``TaskQueue``，但此前**没有任何进程消费该队列**——``run_scheduler`` 只往内存队列里放，
``WorkerPool`` 仅在测试里被使用。结果是时间驱动 Plan 永远不会执行。

本模块提供 ``WorkerPool`` 所需的生产 ``runner``（满足 ``arun`` 契约），
把队列任务落到既有执行链：

    create_suite_run → EventLoop.run_to_completion → ExecutionLog → Order（可选下单）

两点设计约束：

1. **执行前按主键重读 Plan**：任务可能排队期间 Plan 被取消发布/归档，
   跨线程复用旧模型实例也会读到过期数据；非 ``published`` 直接跳过。
2. **DB 访问在同步线程**：``arun`` 用 ``sync_to_async(thread_sensitive=True)`` 包裹，
   绝不从事件循环里直接访问 Django ORM。

下单默认关闭（``order_broker='none'``）：只落库 ``Order``（状态 pending），
需要真实下单时显式传 ``order_broker='gm'``。
"""
from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async

from .engine import SuiteRunner

logger = logging.getLogger(__name__)

#: 可选的下单通道。
ORDER_BROKERS = ('none', 'gm')

__all__ = ['ORDER_BROKERS', 'PlanExecutionService', 'build_execution_service']


class PlanExecutionService:
    """``WorkerPool`` 的生产执行端：``(plan, symbol)`` → SuiteRun / 日志 / 委托单。

    Args:
        case_executor: 可注入的 Case 执行器（测试替身用）。
        broker: 券商适配器；``None`` 表示只落库不下单。
        risk_controller: 风控拦截器；``None`` 表示不做风控校验。
        data_context_builder: 数据上下文构建器；``None`` 表示不预取行情上下文。
        use_threads: ``parallel`` 模式下是否用线程并发。
        suite_runner: 直接注入已构造好的 ``SuiteRunner``（优先于以上各参数）。
        funds_broker: 账户资金查询通道；非 ``None`` 时执行前按 TTL 同步
            ``AccountFundConfig``（见 ``apps.execution.fund_sync``）。
        funds_ttl: 资金数据有效期（秒）；``<= 0`` 表示每次执行都同步。
        funds_capital_basis: 额度口径（``total`` / ``cash`` / ``available``）——
            账户存在本项目未管理的持仓时用 ``cash`` 可避免市值波动扰动额度上限。
    """

    def __init__(self, case_executor=None, broker=None, risk_controller=None,
                 data_context_builder=None, use_threads=True, suite_runner=None,
                 funds_broker=None, funds_ttl=30, funds_capital_basis='total'):
        self.broker = broker
        self.funds_broker = funds_broker
        self.funds_ttl = funds_ttl
        self.funds_capital_basis = funds_capital_basis
        self._suite_runner = suite_runner or SuiteRunner(
            case_executor=case_executor,
            broker=broker,
            risk_controller=risk_controller,
            data_context_builder=data_context_builder,
            use_threads=use_threads,
        )


    @staticmethod
    def _resolve_plan(plan: Any):
        """按主键重读 Plan；不存在或非 ``published`` 时返回 ``None``（跳过执行）。"""
        from apps.plans.models import Plan

        pk = getattr(plan, 'pk', plan)
        try:
            fresh = Plan.objects.select_related('root_suite').get(pk=pk)
        except Plan.DoesNotExist:
            logger.warning('任务对应的 Plan 已不存在，跳过：pk=%s', pk)
            return None
        if fresh.status != 'published':
            logger.info('Plan 非 published（%s），跳过执行：pk=%s', fresh.status, pk)
            return None
        return fresh

    def run(self, plan, symbol, payload=None):
        """同步执行单个任务（创建 SuiteRun 并跑完事件循环）。

        Args:
            plan: ``Plan`` 实例或主键（队列里通常是实例）。
            symbol: 标的代码。
            payload: 触发载荷。

        Returns:
            Any: ``ExecutionLog`` 实例；Plan 被跳过时返回 ``None``。

        Raises:
            Exception: 执行失败时原样抛出（``SuiteRun`` / ``ExecutionLog`` 已落库），
                并在能拿到 run 句柄时补发一条 ``suite_failed`` 告警。
        """
        resolved = self._resolve_plan(plan)
        if resolved is None:
            return None
        self._refresh_funds(resolved)
        captured: dict[str, Any] = {}

        def _capture(run):
            captured['run'] = run

        try:
            return self._suite_runner.run(resolved, symbol, payload, on_run=_capture)
        except Exception as exc:
            self._alert_failure(captured.get('run'), exc)
            raise

    def _refresh_funds(self, plan) -> None:
        """执行前按 TTL 同步账户资金（失败只记日志，不阻断交易）。

        资金同步失败时**不写 0**（``fund_sync`` 保证保留上次值），因此这里
        即使跳过同步，执行链路仍按上一次成功同步的资金上限做额度校验。
        """
        if self.funds_broker is None or not getattr(plan, 'account_id', ''):
            return
        from apps.execution.fund_sync import ensure_funds_fresh

        try:
            ensure_funds_fresh(plan.account_id, self.funds_broker, self.funds_ttl,
                               capital_basis=self.funds_capital_basis)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning('执行前资金同步失败（沿用上次同步值）：%s', exc)

    @staticmethod
    def _alert_failure(run, exc) -> None:
        """执行失败补发告警（告警本身失败只记日志，不影响主流程）。"""
        if run is None:
            return
        try:
            from apps.execution.alerts import alert_service

            alert_service.create_suite_failed_alert(
                suite_run=run,
                error_message=str(exc)[:2000],
                error_code=getattr(exc, 'error_code', None) or 'EXECUTION_FAILED',
            )
        except Exception as alert_exc:  # pylint: disable=broad-except
            logger.warning('创建策略执行失败告警时出错（不影响主流程）：%s', alert_exc)

    async def arun(self, plan, symbol, payload=None):
        """``WorkerPool`` 契约入口：把同步执行挪到同步线程。"""
        return await sync_to_async(self.run, thread_sensitive=True)(plan, symbol, payload)


def build_execution_service(order_broker='none', use_threads=True, enable_risk=True,
                            funds_source='none', funds_refresh_interval=30,
                            account_provider=True, funds_capital_basis='total'):
    """按名称构造生产执行服务（供管理命令使用）。

    下单通道与资金同步通道**独立**且共用同一个 gm 适配器实例：
    ``order_broker='none'`` 时可以只做资金同步（只读账户、不下单），
    反之亦然；行情数据上下文在任一通道开启时都能用上 gm 回退。

    Args:
        order_broker: ``none``（默认，只落库不下单）或 ``gm``（真实下单）。
        use_threads: ``parallel`` 模式下是否用线程并发。
        enable_risk: 是否挂 ``RiskController``（默认挂，做单笔/每日限额与交易时段校验）。
        funds_source: ``none``（默认，不同步）或 ``gm``（按账户查询同步资金）。
        funds_capital_basis: 额度口径（``total`` 默认 / ``cash`` / ``available``）——
            账户存在本项目未管理的持仓时选 ``cash``，避免市值盘中波动扰动额度上限。
        funds_refresh_interval: 资金数据有效期（秒）；``<= 0`` 表示每次执行都同步。
        account_provider: 是否把 gm 账户快照接入风控（使 ``max_account_value`` /
            ``max_position_*`` 这类账户级限额有实时数据）。

    Returns:
        PlanExecutionService: 已装配数据上下文、（可选）下单通道与资金同步通道的执行服务。

    Raises:
        ValueError: ``order_broker`` / ``funds_source`` 不在 :data:`ORDER_BROKERS` 内。
    """
    if order_broker not in ORDER_BROKERS:
        raise ValueError(f'order_broker 必须是 {ORDER_BROKERS} 之一')
    if funds_source not in ORDER_BROKERS:
        raise ValueError(f'funds_source 必须是 {ORDER_BROKERS} 之一')

    broker = None
    if 'gm' in (order_broker, funds_source):
        from .gm_adapter import GmBrokerAdapter

        # GmBrokerAdapter 构造只做 set_serv_addr / set_token，不发起连接
        broker = GmBrokerAdapter()
    logger.info('下单通道：%s（%s）；账户资金同步：%s',
                order_broker,
                '真实提交委托' if order_broker == 'gm' else '委托单只落库',
                funds_source)

    from .fixture import DataContextBuilder
    from .risk import RiskController

    risk_controller = None
    if enable_risk:
        risk_controller = RiskController(
            account_provider=broker if (account_provider and broker) else None,
        )
    return PlanExecutionService(
        broker=broker if order_broker == 'gm' else None,
        risk_controller=risk_controller,
        data_context_builder=DataContextBuilder(broker=broker),
        use_threads=use_threads,
        funds_broker=broker if funds_source == 'gm' else None,
        funds_ttl=funds_refresh_interval,
        funds_capital_basis=funds_capital_basis,
    )
