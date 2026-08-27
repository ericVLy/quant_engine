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

    def test_gm_status_values_are_translated(self):
        self.assertEqual(self.adapter._status(3), 'filled')
        self.assertEqual(self.adapter._status(8), 'rejected')
        self.assertIsNone(self.adapter._status(99))