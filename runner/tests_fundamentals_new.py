"""基本面数据扩展测试 - 验证新的 Provider 抽象 + AkShare 实现。"""
import unittest
from unittest.mock import patch

import pandas as pd

from apps.watchlists.models import Symbol
from runner.fundamentals import (
    AkshareFundamentalsProvider,
    FundamentalsProvider,
    _extract_latest_period,
    _normalize_value,
    normalize_stock_code,
)


class TestNormalizeStockCode(unittest.TestCase):
    def test_removes_prefix(self):
        self.assertEqual(normalize_stock_code('sz000001'), '000001')

    def test_removes_suffix(self):
        self.assertEqual(normalize_stock_code('000001.XSHE'), '000001')

    def test_pads_to_six(self):
        self.assertEqual(normalize_stock_code('1'), '000001')


class TestNormalizeValue(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(_normalize_value(None))

    def test_empty_string(self):
        self.assertIsNone(_normalize_value(''))

    def test_numeric_string(self):
        self.assertEqual(_normalize_value('12345.67'), 12345.67)

    def test_comma_number(self):
        self.assertEqual(_normalize_value('1,234,567'), 1234567.0)

    def test_text(self):
        self.assertEqual(_normalize_value('银行'), '银行')


class TestExtractLatestPeriod(unittest.TestCase):
    def test_empty_frame(self):
        self.assertEqual(_extract_latest_period(pd.DataFrame(), {}), {})

    def test_extracts_latest_period(self):
        frame = pd.DataFrame({
            '20250930': {'净资产收益率(%)': 12.5, '销售毛利率(%)': 35.6},
            '20260930': {'净资产收益率(%)': 13.2, '销售毛利率(%)': 36.1},
        })
        result = _extract_latest_period(
            frame, {'净资产收益率(%)': 'roe', '销售毛利率(%)': 'gross_margin'}
        )
        self.assertEqual(result['roe'], 13.2)
        self.assertEqual(result['gross_margin'], 36.1)
class TestAkshareFundamentalsProviderBasicInfo(unittest.TestCase):
    def setUp(self):
        self.provider = AkshareFundamentalsProvider()
        self.symbol = Symbol(code='000001', name='平安银行', market='A')

    def test_skips_non_a_share(self):
        hk_symbol = Symbol(code='00700', name='腾讯', market='HK')
        with patch('runner.fundamentals.ak.stock_individual_info_em') as fetch:
            self.assertEqual(self.provider.fetch_basic_info(hk_symbol), {})
        fetch.assert_not_called()

    def test_degrades_on_error(self):
        with patch('runner.fundamentals.ak.stock_individual_info_em',
                   side_effect=RuntimeError('network error')):
            self.assertEqual(self.provider.fetch_basic_info(self.symbol), {})

    def test_parses_basic_info(self):
        frame = pd.DataFrame([
            {'item': '总市值', 'value': '123456789'},
            {'item': '行业', 'value': '银行'},
            {'item': '上市时间', 'value': '19910403'},
        ])
        with patch('runner.fundamentals.ak.stock_individual_info_em', return_value=frame):
            result = self.provider.fetch_basic_info(self.symbol)
        self.assertEqual(result['market_cap'], 123456789.0)
        self.assertEqual(result['industry'], '银行')


class TestAkshareFundamentalsProviderFinancialStatements(unittest.TestCase):
    def setUp(self):
        self.provider = AkshareFundamentalsProvider()
        self.symbol = Symbol(code='000001', name='平安银行', market='A')

    def test_fetch_financial_indicators(self):
        frame = pd.DataFrame({'20260930': {'净资产收益率(%)': 12.5}})
        with patch('runner.fundamentals.ak.stock_financial_analysis_indicator', return_value=frame):
            result = self.provider.fetch_financial_indicators(self.symbol)
        self.assertEqual(result['roe'], 12.5)

    def test_fetch_balance_sheet(self):
        frame = pd.DataFrame({'20260930': {'资产总计': 5200000000000.0}})
        with patch('runner.fundamentals.ak.stock_balance_sheet_by_report_em', return_value=frame):
            result = self.provider.fetch_balance_sheet(self.symbol)
        self.assertEqual(result['total_assets'], 5200000000000.0)

    def test_fetch_income_statement(self):
        frame = pd.DataFrame({'20260930': {'净利润': 54000000000.0}})
        with patch('runner.fundamentals.ak.stock_profit_sheet_by_report_em', return_value=frame):
            result = self.provider.fetch_income_statement(self.symbol)
        self.assertEqual(result['net_profit'], 54000000000.0)

    def test_fetch_cash_flow(self):
        frame = pd.DataFrame({'20260930': {'经营活动产生的现金流量净额': 68000000000.0}})
        with patch('runner.fundamentals.ak.stock_cash_flow_sheet_by_report_em', return_value=frame):
            result = self.provider.fetch_cash_flow(self.symbol)
        self.assertEqual(result['net_operating_cash_flow'], 68000000000.0)

    def test_sub_report_failure_does_not_block_others(self):
        info = pd.DataFrame([{'item': '总市值', 'value': '123456789'}])
        balance = pd.DataFrame({'20260930': {'资产总计': 100.0}})
        with patch('runner.fundamentals.ak.stock_individual_info_em', return_value=info), \
             patch('runner.fundamentals.ak.stock_financial_analysis_indicator',
                   side_effect=RuntimeError('network error')), \
             patch('runner.fundamentals.ak.stock_balance_sheet_by_report_em', return_value=balance):
            result = self.provider.fetch(self.symbol)
        self.assertEqual(result['market_cap'], 123456789.0)
        self.assertEqual(result['total_assets'], 100.0)
        self.assertNotIn('roe', result)

    def test_context_structure(self):
        info = pd.DataFrame([{'item': '总市值', 'value': '123456789'}])
        empty = pd.DataFrame()
        with patch('runner.fundamentals.ak.stock_individual_info_em', return_value=info), \
             patch('runner.fundamentals.ak.stock_financial_analysis_indicator', return_value=empty), \
             patch('runner.fundamentals.ak.stock_balance_sheet_by_report_em', return_value=empty), \
             patch('runner.fundamentals.ak.stock_profit_sheet_by_report_em', return_value=empty), \
             patch('runner.fundamentals.ak.stock_cash_flow_sheet_by_report_em', return_value=empty):
            ctx = self.provider.context(self.symbol)
        self.assertEqual(ctx['provider'], 'akshare')
        self.assertEqual(ctx['symbol'], '000001')
        self.assertIn('asof', ctx)
        self.assertIn('metrics', ctx)


class TestFundamentalsProviderAbstract(unittest.TestCase):
    def test_cannot_instantiate_abstract(self):
        with self.assertRaises(TypeError):
            FundamentalsProvider()

    def test_subclass_must_implement_fetch_basic_info(self):
        class IncompleteProvider(FundamentalsProvider):
            name = 'incomplete'
        with self.assertRaises(TypeError):
            IncompleteProvider()


if __name__ == '__main__':
    unittest.main()