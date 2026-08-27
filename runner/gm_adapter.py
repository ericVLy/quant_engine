"""Minimal adapter around the external gm Python SDK."""

from decimal import Decimal

from django.db import transaction

from apps.execution.models import Order


class GmBrokerAdapter:
    """Translate runner operations to ``gm.api`` calls.

    The SDK is imported lazily so the rest of the runner remains usable in
    environments that do not connect to a GoldMiner terminal.
    """

    def __init__(self, token=None, api=None):
        if api is None:
            from gm import api as gm_api
            api = gm_api
        self.api = api
        if token:
            self.api.set_token(token)

    def subscribe(self, symbols, frequency='1d', count=1, fields=None,
                  data_format='df'):
        return self.api.subscribe(
            symbols=symbols, frequency=frequency, count=count,
            fields=fields, format=data_format,
        )

    def history(self, symbol, frequency, start_time, end_time, fields=None,
                adjust=None, data_frame=False):
        return self.api.history(
            symbol=symbol, frequency=frequency, start_time=start_time,
            end_time=end_time, fields=fields, adjust=adjust, df=data_frame,
        )

    def history_n(self, symbol, frequency, count, end_time=None, fields=None,
                  adjust=None, data_frame=False):
        return self.api.history_n(
            symbol=symbol, frequency=frequency, count=count,
            end_time=end_time, fields=fields, adjust=adjust, df=data_frame,
        )

    def schedule(self, callback, date_rule='1d', time_rule='09:30:00'):
        return self.api.schedule(
            schedule_func=callback, date_rule=date_rule, time_rule=time_rule,
        )

    def submit_order(self, symbol, order_data):
        direction = order_data.get('direction')
        side_name = 'OrderSide_Buy' if direction == 'buy' else 'OrderSide_Sell'
        order_type_name = 'OrderType_Market' if order_data.get('order_type', 'market') == 'market' else 'OrderType_Limit'
        position_name = order_data.get('position_effect', 'open')
        position_effect_name = 'PositionEffect_Close' if position_name == 'close' else 'PositionEffect_Open'
        missing = [key for key in ('volume',) if key not in order_data]
        if missing or direction not in ('buy', 'sell'):
            raise ValueError('订单必须包含合法 direction 和 volume')
        return self.api.order_volume(
            symbol=symbol,
            volume=int(order_data['volume']),
            side=getattr(self.api, side_name),
            order_type=getattr(self.api, order_type_name),
            position_effect=getattr(self.api, position_effect_name),
            price=float(order_data.get('price', 0)),
        )

    def get_orders(self):
        return self.api.get_orders()

    @staticmethod
    def _status(value):
        return {
            1: 'sent',
            2: 'sent',
            3: 'filled',
            8: 'rejected',
            5: 'rejected',
        }.get(value)

    @transaction.atomic
    def on_order_status(self, report):
        """Apply a gm order report to a local Order, when identifiable."""
        local_order = Order.objects.filter(
            symbol=report.get('symbol', ''),
            status__in=('pending', 'sent'),
        ).order_by('-created_at').first()
        if local_order is None:
            return None
        status = self._status(report.get('status'))
        if status:
            local_order.status = status
        if report.get('price') is not None:
            local_order.price = Decimal(str(report['price']))
        if status or report.get('price') is not None:
            local_order.save(update_fields=['status', 'price', 'updated_at'])
        return local_order

    def on_error(self, callback):
        """Return a callback suitable for a host strategy's error hook."""
        return callback