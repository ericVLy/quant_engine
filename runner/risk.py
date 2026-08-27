class RiskDecision:
    def __init__(self, allowed, reason=''):
        self.allowed = allowed
        self.reason = reason


class RiskController:
    """Apply simple per-order limits before an external order is submitted."""

    def __init__(self, max_volume=None, max_value=None):
        self.max_volume = max_volume
        self.max_value = max_value

    def check(self, order_data):
        volume = int(order_data.get('volume', 0))
        price = float(order_data.get('price', 0))
        if volume <= 0:
            return RiskDecision(False, '订单 volume 必须大于 0')
        if self.max_volume is not None and volume > self.max_volume:
            return RiskDecision(False, '订单数量超过风控上限')
        if self.max_value is not None and volume * price > self.max_value:
            return RiskDecision(False, '订单金额超过风控上限')
        return RiskDecision(True)