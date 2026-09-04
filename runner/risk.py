"""风控拦截器。

在 Executor 节点输出（委托单）真正提交到外部券商前做多层校验：

- 数量与金额上限（单笔）；
- 单向持仓限制（仅持多头 / 仅持空头 / 禁止做空）；
- 每日累计成交金额上限（基于本地已成交 Order 统计）；
- 交易时段校验（可配置允许时段，例如仅交易时段下单）。

每个拦截器的决策以 ``RiskDecision`` 表达，wrapper 聚合后给出最终结论。
"""

from datetime import datetime, time as dtime

from django.db.models import Sum
from django.utils import timezone

from apps.execution.models import Order


class RiskDecision:
    def __init__(self, allowed, reason=''):
        self.allowed = allowed
        self.reason = reason


class TradeTimeWindow:
    """可配置的交易时段窗口（默认 A股 9:30-11:30 / 13:00-15:00）。"""

    def __init__(self, sessions=None):
        # 接受两种写法：
        #   [(9, 30, 11, 30), (13, 0, 15, 0)]  扁平 (start_hh,start_mm,end_hh,end_mm)
        #   [((9, 30), (11, 30)), ((13, 0), (15, 0))]  嵌套 (start, end)
        self.sessions = []
        for session in (sessions if sessions is not None else
                        [(9, 30, 11, 30), (13, 0, 15, 0)]):
            if len(session) == 4:
                self.sessions.append(((session[0], session[1]), (session[2], session[3])))
            elif len(session) == 2 and isinstance(session[0], (tuple, list)):
                self.sessions.append((tuple(session[0]), tuple(session[1])))
            else:
                raise ValueError(f'非法交易时段配置: {session}')

    def allows(self, when=None):
        when = when or timezone.localtime(timezone.now())
        if hasattr(when, 'weekday') and when.weekday() >= 5:
            return False
        current = when.time()
        return any(self._inside(current, start, end) for start, end in self.sessions)

    @staticmethod
    def _inside(current, start, end):
        start_t = dtime(*start)
        end_t = dtime(*end)
        return start_t <= current <= end_t


class PositionPolicy:
    """单向持仓方向限制。"""

    def __init__(self, mode='both', max_volume=None, max_value=None):
        # mode: both / long_only / short_only / flat
        self.mode = mode
        self.max_volume = max_volume
        self.max_value = max_value

    def check(self, order_data):
        direction = order_data.get('direction')
        volume = int(order_data.get('volume', 0))
        price = float(order_data.get('price', 0))

        if volume <= 0:
            return RiskDecision(False, '订单 volume 必须大于 0')
        if self.max_volume is not None and volume > self.max_volume:
            return RiskDecision(False, '订单数量超过风控上限')
        if self.max_value is not None and volume * price > self.max_value:
            return RiskDecision(False, '订单金额超过风控上限')

        if self.mode == 'long_only' and direction == 'sell':
            return RiskDecision(False, 'long_only 模式禁止卖出/做空')
        if self.mode == 'short_only' and direction == 'buy':
            return RiskDecision(False, 'short_only 模式禁止买入/做多')
        if self.mode == 'flat':
            return RiskDecision(False, 'flat 模式禁止开仓')
        return RiskDecision(True)


class DailyLimitPolicy:
    """每日累计成交金额上限（基于本地最近 24h 的已成交/已发送订单）。"""

    def __init__(self, max_daily_value=None, on_date=None):
        self.max_daily_value = max_daily_value
        self.on_date = on_date

    def _cumulative(self, order_data):
        today_key = timezone.localdate()
        base_value = Order.objects.filter(
            created_at__date=today_key,
            status__in=('pending', 'sent', 'filled'),
        ).aggregate(total=Sum('price'))['total'] or 0
        incoming_value = int(order_data.get('volume', 0)) * float(order_data.get('price', 0))
        return base_value + incoming_value

    def check(self, order_data):
        if self.max_daily_value is None:
            return RiskDecision(True)
        cumulative = self._cumulative(order_data)
        if cumulative > self.max_daily_value:
            return RiskDecision(
                False,
                f'每日累计金额 {cumulative:.2f} 超过限额 {self.max_daily_value:.2f}',
            )
        return RiskDecision(True)


class RiskController:
    """聚合多个风控策略，全部通过才允许下单。"""

    def __init__(self, max_volume=None, max_value=None, position_mode='both',
                 allowed_sessions=None, max_daily_value=None):
        self.position_policy = PositionPolicy(
            mode=position_mode, max_volume=max_volume, max_value=max_value,
        )
        self.trade_window = TradeTimeWindow(
            sessions=allowed_sessions if allowed_sessions is not None
            else [(9, 30, 11, 30), (13, 0, 15, 0)]
        )
        self.daily_limit = DailyLimitPolicy(max_daily_value=max_daily_value)

    def check(self, order_data):
        if not self.trade_window.allows():
            return RiskDecision(False, '当前不在交易时段')
        decision = self.position_policy.check(order_data)
        if not decision.allowed:
            return decision
        decision = self.daily_limit.check(order_data)
        if not decision.allowed:
            return decision
        return RiskDecision(True)