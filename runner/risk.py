"""风控拦截器。

在 Executor 节点输出（委托单）真正提交到外部券商前做多层校验：

- 数量与金额上限（单笔）；
- 单向持仓限制（仅持多头 / 仅持空头 / 禁止做空）；
- 每日累计成交金额上限（基于本地已成交 Order 统计）；
- 交易时段校验（可配置允许时段，例如仅交易时段下单）。

每个拦截器的决策以 ``RiskDecision`` 表达，wrapper 聚合后给出最终结论。
"""

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from django.db.models import Sum
from django.utils import timezone

from apps.execution.models import Order

#: 各市场交易时段的判定时区（与 monitoring 模块的 zoneinfo 口径一致）。
MARKET_TIMEZONES = {
    'A': 'Asia/Shanghai',
    'HK': 'Asia/Hong_Kong',
    'US': 'America/New_York',
}

#: 缺省市场时区：默认窗口（9:30-11:30 / 13:00-15:00）是 A 股口径。
DEFAULT_TRADE_TIMEZONE = MARKET_TIMEZONES['A']


def resolve_market_timezone(symbol=None, market=None):
    """解析交易时段判定时区（库内 ``Symbol.market`` 优先，回退前缀推断）。

    Args:
        symbol: 标的代码（如 ``000001`` / ``600000``）。
        market: 显式市场（``A`` / ``HK`` / ``US``），优先于 ``symbol``。

    Returns:
        str: IANA 时区名；无法识别时回退 :data:`DEFAULT_TRADE_TIMEZONE`。

    风控不得因时区解析失败而中断，故任何异常（如库内无该标的）都回退默认值。
    """
    name = market
    if not name and symbol:
        code = str(symbol).strip()
        try:
            from apps.watchlists.models import Symbol

            name = Symbol.objects.filter(code=code).values_list('market', flat=True).first()
        except Exception:  # pylint: disable=broad-except
            name = None
        if not name:
            try:
                from apps.watchlists.services import infer_market_from_code

                name = infer_market_from_code(code)
            except Exception:  # pylint: disable=broad-except
                name = None
    return MARKET_TIMEZONES.get(name or '', DEFAULT_TRADE_TIMEZONE)


class RiskDecision:
    def __init__(self, allowed, reason=''):
        self.allowed = allowed
        self.reason = reason


class TradeTimeWindow:
    """可配置的交易时段窗口（默认 A股 9:30-11:30 / 13:00-15:00）。

    时段按**市场时区**判定（默认 ``Asia/Shanghai``，见 :data:`MARKET_TIMEZONES`）。
    历史实现直接用 ``timezone.localtime(timezone.now())``，而 ``TIME_ZONE='UTC'``
    时它给出的是 UTC 墙钟，使默认窗口实际生效为「北京时间 17:30-19:30」——
    **A 股盘中下单被拦、A 股休市反而放行**（v2.14 修复）。
    """

    def __init__(self, sessions=None, timezone_name=None):
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
        self.timezone_name = timezone_name or DEFAULT_TRADE_TIMEZONE

    def allows(self, when=None, timezone_name=None):
        """判断给定时点是否在允许时段内。

        Args:
            when: 判定时点；``None`` 取当前时间。aware 时间先换算到
                ``timezone_name`` 所在时区；naive 时间视为该时区的本地墙钟
                （保持既有调用契约，便于测试注入）。
            timezone_name: 覆盖实例时区（按标的所属市场传入）。

        Returns:
            bool: 是否允许交易；周六 / 周日一律不允许。
        """
        zone = ZoneInfo(timezone_name or self.timezone_name)
        when = when if when is not None else timezone.localtime(timezone.now())
        if timezone.is_aware(when):
            when = when.astimezone(zone)
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
                 allowed_sessions=None, max_daily_value=None,
                 max_account_value=None, max_position_value=None,
                 max_position_volume=None, account_provider=None,
                 trade_timezone=None):
        self.position_policy = PositionPolicy(
            mode=position_mode, max_volume=max_volume, max_value=max_value,
        )
        # 显式指定时全局生效；缺省按每笔订单的 symbol 所属市场动态判定
        self.trade_timezone = trade_timezone
        self.trade_window = TradeTimeWindow(
            sessions=allowed_sessions if allowed_sessions is not None
            else [(9, 30, 11, 30), (13, 0, 15, 0)],
            timezone_name=trade_timezone,
        )
        self.daily_limit = DailyLimitPolicy(max_daily_value=max_daily_value)
        self.max_account_value = max_account_value
        self.max_position_value = max_position_value
        self.max_position_volume = max_position_volume
        self.account_provider = account_provider

    def _account_snapshot(self):
        if self.account_provider is None:
            return {}, []
        account = self.account_provider.get_account() or {}
        positions = self.account_provider.get_positions() or []
        return account, positions

    def _trade_timezone(self, order_data):
        """本笔订单的时段判定时区：显式配置优先，否则按 symbol 所属市场判定。"""
        if self.trade_timezone:
            return self.trade_timezone
        return resolve_market_timezone(symbol=order_data.get('symbol'))

    def check(self, order_data):
        if not self.trade_window.allows(timezone_name=self._trade_timezone(order_data)):
            return RiskDecision(False, '当前不在交易时段')
        decision = self.position_policy.check(order_data)
        if not decision.allowed:
            return decision
        decision = self.daily_limit.check(order_data)
        if not decision.allowed:
            return decision
        account, positions = self._account_snapshot()
        order_value = float(order_data.get('price', 0)) * int(order_data.get('volume', 0))
        available = account.get('available') or account.get('cash')
        if order_data.get('direction') == 'buy' and self.max_account_value is not None:
            if available is not None and float(available) < order_value:
                return RiskDecision(False, '账户可用资金不足')
        if self.max_position_value is not None or self.max_position_volume is not None:
            position_value = 0.0
            position_volume = 0
            for position in positions:
                position_value += float(position.get('market_value', position.get('value', 0)) or 0)
                position_volume += int(position.get('volume', position.get('quantity', 0)) or 0)
            direction_factor = 1 if order_data.get('direction') == 'buy' else -1
            projected_value = max(0.0, position_value + direction_factor * order_value)
            projected_volume = max(0, position_volume + direction_factor * int(order_data.get('volume', 0)))
            if projected_value > (self.max_position_value or float('inf')):
                return RiskDecision(False, '账户总仓位金额超过风控上限')
            if projected_volume > (self.max_position_volume or float('inf')):
                return RiskDecision(False, '账户总仓位数量超过风控上限')
        return RiskDecision(True)