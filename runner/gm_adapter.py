"""Minimal adapter around the external gm Python SDK."""

from decimal import Decimal

from django.db import transaction

from apps.execution.models import Order
from django.conf import settings


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
        detault_token = getattr(settings, 'gm_token', None)
        if token:
            self.api.set_token(token)
        elif detault_token:
            self.api.set_token(detault_token)

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

    def get_account(self):
        """Return the broker account snapshot when the SDK exposes it."""
        getter = getattr(self.api, 'get_cash', None) or getattr(self.api, 'get_account', None)
        return getter() if getter else {}

    def get_positions(self):
        getter = getattr(self.api, 'get_positions', None)
        return getter() if getter else []

    @staticmethod
    def _status(value):
        if hasattr(value, 'value'):
            value = value.value
        if isinstance(value, str):
            return {
                'new': 'pending', 'pending': 'pending', 'created': 'pending',
                'submitted': 'sent', 'sent': 'sent', 'accepted': 'sent',
                'partial_filled': 'sent', 'part_filled': 'sent',
                'filled': 'filled', 'completed': 'filled',
                'rejected': 'rejected', 'reject': 'rejected',
                'canceled': 'canceled', 'cancelled': 'canceled',
            }.get(value.strip().lower())
        return {
            0: 'pending',
            1: 'sent',
            2: 'sent',
            3: 'filled',
            8: 'rejected',
            5: 'rejected',
            6: 'canceled',
        }.get(value)

    @staticmethod
    def _status_rank(status):
        return {'pending': 0, 'sent': 1, 'canceled': 2, 'rejected': 2, 'filled': 3}.get(status, -1)

    @staticmethod
    def _report_value(report, *names):
        for name in names:
            value = report.get(name) if isinstance(report, dict) else getattr(report, name, None)
            if value is not None:
                return value
        return None

    @transaction.atomic
    def on_order_status(self, report):
        """Apply a gm order report to a local Order, when identifiable."""
        external_id = self._report_value(report, 'cl_ord_id', 'order_id', 'order_id_str')
        local_order = Order.objects.filter(external_order_id=external_id).first()
        if local_order is None:
            local_order = Order.objects.filter(
                symbol=self._report_value(report, 'symbol') or '',
                status__in=('pending', 'sent'),
            ).order_by('-created_at').first()
        if local_order is None:
            return None
        status = self._status(self._report_value(report, 'status', 'order_status'))
        if status and self._status_rank(status) >= self._status_rank(local_order.status):
            local_order.status = status
        price = self._report_value(report, 'price', 'filled_price', 'avg_price')
        if price is not None:
            local_order.price = Decimal(str(price))
        filled_volume = self._report_value(report, 'filled_volume', 'filled_qty', 'filled_quantity')
        if filled_volume is not None:
            local_order.filled_volume = max(
                local_order.filled_volume,
                max(0, min(int(filled_volume), local_order.volume)),
            )
        report_data = dict(report) if isinstance(report, dict) else {
            key: getattr(report, key) for key in ('cl_ord_id', 'order_id', 'symbol', 'status', 'price')
            if getattr(report, key, None) is not None
        }
        local_order.report_payload = report_data
        if external_id and local_order.external_order_id != str(external_id):
            local_order.external_order_id = str(external_id)
        if status or price is not None or external_id or filled_volume is not None:
            local_order.save(update_fields=[
                'status', 'price', 'external_order_id', 'filled_volume',
                'report_payload', 'updated_at',
            ])
        return local_order

    def on_error(self, callback):
        """Return a callback suitable for a host strategy's error hook."""
        return callback