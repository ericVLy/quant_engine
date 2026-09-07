from unittest.mock import Mock

from django.test import SimpleTestCase

from .gm_adapter import GmBrokerAdapter


class GmBrokerAdapterTest(SimpleTestCase):
    def setUp(self):
        self.api = Mock(
            OrderSide_Buy=1,
            OrderSide_Sell=2,
            OrderType_Market=2,
            OrderType_Limit=1,
            PositionEffect_Open=1,
            PositionEffect_Close=2,
        )
        self.adapter = GmBrokerAdapter(api=self.api)

    def test_selected_market_data_interfaces_are_forwarded(self):
        self.adapter.subscribe('SHSE.600000', frequency='60s', count=50)
        self.api.subscribe.assert_called_once_with(
            symbols='SHSE.600000', frequency='60s', count=50,
            fields=None, format='df',
        )

        self.adapter.history('SHSE.600000', '1d', '2026-01-01', '2026-01-02')
        self.api.history.assert_called_once_with(
            symbol='SHSE.600000', frequency='1d', start_time='2026-01-01',
            end_time='2026-01-02', fields=None, adjust=None, df=False,
        )

    def test_order_volume_maps_runner_order_to_gm_constants(self):
        self.adapter.submit_order(
            'SHSE.600000', {'direction': 'buy', 'price': '12.34', 'volume': 200}
        )

        self.api.order_volume.assert_called_once_with(
            symbol='SHSE.600000', volume=200, side=1, order_type=2,
            position_effect=1, price=12.34,
        )

    def test_account_and_position_interfaces_are_forwarded(self):
        self.api.get_cash.return_value = {'available': 1000}
        self.api.get_positions.return_value = [{'symbol': 'SHSE.600000', 'volume': 100}]
        self.assertEqual(self.adapter.get_account(), {'available': 1000})
        self.assertEqual(self.adapter.get_positions()[0]['volume'], 100)

    def test_gm_status_values_are_translated(self):
        self.assertEqual(self.adapter._status(3), 'filled')
        self.assertEqual(self.adapter._status(8), 'rejected')
        self.assertEqual(self.adapter._status(5), 'canceled')
        self.assertEqual(self.adapter._status(10), 'pending')
        self.assertEqual(self.adapter._status(6), 'sent')
        self.assertIsNone(self.adapter._status(99))

    def test_order_report_does_not_regress_filled_order(self):
        self.assertEqual(self.adapter._status_rank('filled'), 3)
        self.assertGreater(self.adapter._status_rank('filled'), self.adapter._status_rank('sent'))

    def test_request_cancel_uses_wait_cancel_orders_contract(self):
        self.api.order_cancel.return_value = {'cl_ord_id': 'gm-cxl-1', 'status': 'canceled'}
        self.adapter.account_id = 'acct-1'
        result = self.adapter.request_cancel('SHSE.600000', order_id='gm-cxl-1')
        self.api.order_cancel.assert_called_once_with(
            [{'cl_ord_id': 'gm-cxl-1', 'account_id': 'acct-1'}],
        )
        self.assertEqual(result['status'], 'canceled')

    def test_request_cancel_requires_order_id(self):
        self.adapter.account_id = 'acct-1'
        with self.assertRaises(ValueError):
            self.adapter.request_cancel('SHSE.600000')

    def test_request_cancel_raises_when_sdk_has_no_cancel_api(self):
        class _BareSDK:
            OrderSide_Buy = 1
            OrderSide_Sell = 2

            def set_token(self, token):
                return None
        adapter = GmBrokerAdapter(api=_BareSDK())
        with self.assertRaises(NotImplementedError):
            adapter.request_cancel('SHSE.600000', order_id='gm-1')