"""Minimal adapter around the external gm Python SDK."""

from decimal import Decimal
import os

from django.db import transaction

from apps.execution.models import Order
from django.conf import settings


class GmBrokerAdapter:
    """Translate runner operations to ``gm.api`` calls.

    The SDK is imported lazily so the rest of the runner remains usable in
    environments that do not connect to a GoldMiner terminal.
    """

    def __init__(self, token=None, api=None, account_id=None):
        if api is None:
            from gm import api as gm_api
            api = gm_api
        self.api = api
        default_token = self._default_token()
        if token:
            self.api.set_token(token)
        elif default_token:
            self.api.set_token(default_token)
        self.account_id = account_id
        if account_id:
            self.set_account_id(account_id)

    @staticmethod
    def _default_token():
        # 注意：Django settings 仅暴露大写配置名；历史小写 `gm_token` 不会被
        # 载入 settings 对象。优先读取大写 GM_TOKEN，回退到环境变量。
        return (getattr(settings, 'GM_TOKEN', None)
                or getattr(settings, 'gm_token', None)
                or os.getenv('GM_TOKEN', '') or None)

    def set_account_id(self, account_id):
        """绑定交易账户（模拟账户），后续下单/查询均作用于该账户。"""
        setter = getattr(self.api, 'set_account_id', None)
        if setter is None:
            raise NotImplementedError('gm SDK 未暴露 set_account_id；无法绑定交易账户')
        setter(account_id)
        self.account_id = account_id
        return self.account_id

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

    def request_cancel(self, symbol=None, order_data=None, order_id=None):
        """提交撤单请求（针对外部订单号）。

        真实 gm SDK 契约：``order_cancel(wait_cancel_orders)``，其中
        ``wait_cancel_orders`` 是形如
        ``[{'cl_ord_id': <外部订单号>, 'account_id': <账户ID>}]`` 的列表。
        保留旧版 ``symbol`` 位置参数兼容性，但不再按 ``{symbol, order_id}``
        关键字转发（真实接口不接受该契约）。
        """
        order_data = order_data or {}
        order_id = order_id or order_data.get('external_order_id')
        cancel = (
            getattr(self.api, 'order_cancel', None)
            or getattr(self.api, 'cancel_order', None)
        )
        if cancel is None:
            raise NotImplementedError('gm SDK 未暴露取消委托接口')
        if order_id is None:
            raise ValueError('撤单必须提供外部订单号 order_id')
        wait_orders = [{
            'cl_ord_id': str(order_id),
            'account_id': self.account_id,
        }]
        try:
            return cancel(wait_orders)
        except TypeError:
            # 兼容老版本可能存在的别名契约
            return cancel(wait_cancel_orders=wait_orders)

    def get_account(self):
        """Return the broker account snapshot when the SDK exposes it.

        真实 gm SDK 的 ``get_cash(account_id='')`` 在未显式指定账户时可能
        一直阻塞到 1022 超时；因此绑定账户后始终显式传入 account_id。
        """
        getter = getattr(self.api, 'get_cash', None) or getattr(self.api, 'get_account', None)
        if getter is None:
            return {}
        if self.account_id:
            try:
                return getter(account_id=self.account_id)
            except TypeError:
                return getter()
        return getter()

    def get_positions(self):
        getter = getattr(self.api, 'get_positions', None)
        if getter is None:
            return []
        if self.account_id:
            try:
                return getter(account_id=self.account_id)
            except TypeError:
                return getter()
        return getter()

    def get_unfinished_orders(self):
        """Return currently unfinished (open) orders from the broker."""
        getter = getattr(self.api, 'get_unfinished_orders', None)
        if getter is None:
            return []
        if self.account_id:
            try:
                return getter(account_id=self.account_id)
            except TypeError:
                return getter()
        return getter()

    def get_execution_reports(self, order_id=None):
        """查询成交回报；可按外部订单号（cl_ord_id/order_id）过滤。"""
        getter = getattr(self.api, 'get_execution_reports', None)
        if getter is None:
            return []
        reports = getter()
        if order_id is None:
            return reports
        normalized = reports if isinstance(reports, list) else [reports]
        result = []
        for report in normalized:
            rid = self._report_value(
                report, 'cl_ord_id', 'order_id', 'order_id_str',
            )
            if rid is not None and str(rid) == str(order_id):
                result.append(report)
        return result

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
            10: 'pending',   # PendingNew 待报
            1: 'sent',       # New 已报
            2: 'sent',       # PartiallyFilled 部分成交
            6: 'sent',       # PendingCancel 待撤（订单仍存活）
            3: 'filled',     # Filled 全部成交
            5: 'canceled',   # Canceled 已撤
            8: 'rejected',   # Rejected 已拒
        }.get(value)

    @staticmethod
    def _status_rank(status):
        return {'pending': 0, 'sent': 1, 'canceled': 2, 'rejected': 2, 'filled': 3}.get(status, -1)

    @staticmethod
    def _jsonable(data):
        """将回报中的 datetime/Decimal 等对象转换为可 JSON 序列化的值。"""
        import datetime as _dt
        import decimal as _decimal
        out = {}
        for key, value in (data or {}).items():
            if isinstance(value, _dt.datetime | _dt.date):
                out[key] = value.isoformat()
            elif isinstance(value, _decimal.Decimal):
                out[key] = float(value)
            else:
                out[key] = value
        return out

    @staticmethod
    def _report_value(report, *names):
        for name in names:
            value = report.get(name) if isinstance(report, dict) else getattr(report, name, None)
            if value is not None:
                return value
        return None

    @transaction.atomic
    def on_order_status(self, report):
        """Apply a gm order report to a local Order, when identifiable.

        覆盖完整订单生命周期：受理(→sent)、部分成交、完全成交、拒单、撤单，
        并对<b>重复回报</b>做幂等去重（基于外部订单号 + 状态 + 累计成交量 + 价格
        的指纹）。累计成交达到委托数量时会自动将状态推进为 ``filled``。
        """
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
        price = self._report_value(report, 'price', 'filled_price', 'avg_price')
        filled_volume = self._take_filled_volume(report, local_order)
        # 重复回报幂等去重：相同的 (外部订单号, 状态, 累计成交量, 价格) 只处理一次
        fingerprint = self._report_fingerprint(
            external_id, status or '', filled_volume, price,
        )
        processed = list(local_order.processed_report_keys or [])
        is_duplicate = bool(fingerprint) and fingerprint in processed
        if fingerprint and not is_duplicate:
            processed.append(fingerprint)

        if status and self._status_rank(status) >= self._status_rank(local_order.status):
            local_order.status = status
        # 部分成交累加达到委托总量 → 自动推进为已成交（仅从 pending/sent 出发）
        if (
            filled_volume >= local_order.volume
            and local_order.status in ('pending', 'sent')
        ):
            local_order.status = 'filled'
        if price is not None:
            local_order.price = Decimal(str(price))
        local_order.filled_volume = max(local_order.filled_volume, filled_volume)
        report_data = dict(report) if isinstance(report, dict) else {
            key: getattr(report, key) for key in ('cl_ord_id', 'order_id', 'symbol', 'status', 'price')
            if getattr(report, key, None) is not None
        }
        # 真实回报含 datetime 等不可 JSON 序列化对象，统一转为字符串
        report_data = self._jsonable(report_data)
        payload = dict(local_order.report_payload or {})
        payload.update(report_data)
        payload['duplicate'] = is_duplicate
        payload['fingerprint'] = fingerprint
        local_order.report_payload = payload
        local_order.processed_report_keys = processed
        if external_id and local_order.external_order_id != str(external_id):
            local_order.external_order_id = str(external_id)
        local_order.save(update_fields=[
            'status', 'price', 'external_order_id', 'filled_volume',
            'report_payload', 'processed_report_keys', 'updated_at',
        ])
        return local_order

    @staticmethod
    def _take_filled_volume(report, local_order):
        value = GmBrokerAdapter._report_value(
            report, 'filled_volume', 'filled_qty', 'filled_quantity', 'cum_qty',
        )
        if value is None:
            return local_order.filled_volume
        return max(0, min(int(value), local_order.volume))

    @staticmethod
    def _report_fingerprint(external_id, status, filled_volume, price):
        if not external_id:
            return ''
        parts = [str(external_id)]
        if status:
            parts.append(status)
        if filled_volume is not None:
            parts.append(str(filled_volume))
        if price is not None:
            parts.append(str(price))
        return '|'.join(parts)

    def on_error(self, callback):
        """Return a callback suitable for a host strategy's error hook."""
        return callback