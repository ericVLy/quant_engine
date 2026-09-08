"""基本面数据适配器。

Provider 模式：抽象基类 + 多源实现。各子报表独立降级，字段名统一为英文契约。
"""
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime

import akshare as ak

logger = logging.getLogger(__name__)


BASIC_INFO_MAP = {
    "股票代码": "symbol",
    "股票简称": "name",
    "总股本": "shares_outstanding",
    "流通股": "shares_float",
    "总市值": "market_cap",
    "流通市值": "float_market_cap",
    "行业": "industry",
    "上市时间": "listing_date",
}

FINANCIAL_INDICATOR_MAP = {
    "净资产收益率(%)": "roe",
    "总资产报酬率(%)": "roa",
    "销售毛利率(%)": "gross_margin",
    "销售净利率(%)": "net_margin",
    "资产负债率(%)": "debt_to_equity",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "存货周转率(次)": "inventory_turnover",
    "应收账款周转率(次)": "receivables_turnover",
    "总资产周转率(次)": "total_asset_turnover",
    "已获利息倍数": "interest_coverage",
    "每股经营活动产生的现金流量净额": "operating_cash_flow_per_share",
    "基本每股收益": "eps",
    "每股净资产": "bps",
    "股息率": "dividend_yield",
    "分红比例": "payout_ratio",
    "营业收入同比增长率(%)": "revenue_growth_yoy",
    "净利润同比增长率(%)": "net_profit_growth_yoy",
    "经营活动产生的现金流量净额同比增长率(%)": "operating_cash_flow_growth_yoy",
}

BALANCE_SHEET_MAP = {
    "资产总计": "total_assets",
    "负债合计": "total_liabilities",
    "所有者权益合计": "total_equity",
    "货币资金": "cash_and_equivalents",
    "短期借款": "short_term_borrowings",
    "长期借款": "long_term_borrowings",
    "应收账款": "accounts_receivable",
    "存货": "inventory",
    "固定资产": "fixed_assets",
    "无形资产": "intangible_assets",
    "商誉": "goodwill",
}

INCOME_STATEMENT_MAP = {
    "营业总收入": "total_revenue",
    "营业收入": "operating_revenue",
    "营业成本": "cost_of_revenue",
    "毛利润": "gross_profit",
    "营业费用": "operating_expenses",
    "销售费用": "selling_expenses",
    "管理费用": "admin_expenses",
    "研发费用": "rd_expenses",
    "财务费用": "financial_expenses",
    "营业利润": "operating_profit",
    "利润总额": "total_profit",
    "所得税费用": "income_tax_expense",
    "净利润": "net_profit",
    "归属于母公司所有者的净利润": "net_profit_attributable",
    "少数股东损益": "non_controlling_interests",
}

CASH_FLOW_MAP = {
    "经营活动产生的现金流量净额": "net_operating_cash_flow",
    "投资活动产生的现金流量净额": "net_investing_cash_flow",
    "筹资活动产生的现金流量净额": "net_financing_cash_flow",
    "购建固定资产、无形资产和其他长期资产支付的现金": "capital_expenditures",
    "自由现金流量": "free_cash_flow",
    "分配股利、利润或偿付利息支付的现金": "dividend_paid",
}
def normalize_stock_code(code):
    value = str(code or "").strip().upper()
    value = re.sub(r"^(SH|SZ|BJ)", "", value)
    value = re.sub(r"\.(XSHG|XSHE|SH|SZ)$", "", value)
    return value.zfill(6) if value.isdigit() else value


def _normalize_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        numeric = text.replace(",", "")
        try:
            return float(numeric)
        except ValueError:
            return text
    return value.item() if hasattr(value, "item") else value


def _extract_latest_period(frame, field_map):
    if frame is None or frame.empty:
        return {}
    latest = frame.iloc[:, -1]
    metrics = {}
    for chinese_name, english_name in field_map.items():
        if chinese_name in latest.index:
            value = latest[chinese_name]
            if value is not None:
                metrics[english_name] = _normalize_value(value)
    return metrics


class FundamentalsProvider(ABC):
    name: str = "abstract"
    supports_financial_indicators = True
    supports_balance_sheet = True
    supports_income_statement = True
    supports_cash_flow = True

    def fetch(self, symbol) -> dict:
        metrics = {}
        try:
            metrics.update(self.fetch_basic_info(symbol))
        except Exception as exc:
            logger.warning("%s fetch_basic_info 失败: %s", self.name, exc)
            return {}
        fetchers = [
            ("financial_indicators", self.supports_financial_indicators, self.fetch_financial_indicators),
            ("balance_sheet", self.supports_balance_sheet, self.fetch_balance_sheet),
            ("income_statement", self.supports_income_statement, self.fetch_income_statement),
            ("cash_flow", self.supports_cash_flow, self.fetch_cash_flow),
        ]
        for label, enabled, fn in fetchers:
            if not enabled:
                continue
            try:
                result = fn(symbol)
                if result:
                    metrics.update(result)
            except Exception as exc:
                logger.warning("%s fetch %s 失败 %s: %s", self.name, label,
                               getattr(symbol, "code", symbol), exc)
        return metrics

    @abstractmethod
    def fetch_basic_info(self, symbol) -> dict:
        ...

    def fetch_financial_indicators(self, symbol) -> dict:
        return {}

    def fetch_balance_sheet(self, symbol) -> dict:
        return {}

    def fetch_income_statement(self, symbol) -> dict:
        return {}

    def fetch_cash_flow(self, symbol) -> dict:
        return {}

    def context(self, symbol) -> dict:
        return {
            "provider": self.name,
            "symbol": normalize_stock_code(getattr(symbol, "code", symbol)),
            "asof": datetime.now().isoformat(),
            "metrics": self.fetch(symbol),
        }
class AkshareFundamentalsProvider(FundamentalsProvider):
    name = "akshare"

    def _is_a_share(self, symbol):
        if symbol is None:
            return False
        return str(getattr(symbol, "market", "")).upper() == "A"

    def _fetch_code(self, symbol):
        return normalize_stock_code(getattr(symbol, "code", symbol))

    def fetch_basic_info(self, symbol) -> dict:
        if not self._is_a_share(symbol):
            return {}
        code = self._fetch_code(symbol)
        try:
            frame = ak.stock_individual_info_em(symbol=code)
            if frame is None or frame.empty:
                return {}
            metrics = {}
            for row in frame.itertuples(index=False):
                item = str(getattr(row, "item", row[0])).strip()
                value = row[1] if len(row) > 1 else None
                field = BASIC_INFO_MAP.get(item)
                if field:
                    metrics[field] = _normalize_value(value)
            return metrics
        except Exception as exc:
            logger.warning("AkShare 基础信息获取失败 %s: %s", code, exc)
            return {}

    def fetch_financial_indicators(self, symbol) -> dict:
        if not self._is_a_share(symbol):
            return {}
        code = self._fetch_code(symbol)
        try:
            frame = ak.stock_financial_analysis_indicator(symbol=code)
            if frame is None or frame.empty:
                return {}
            return _extract_latest_period(frame, FINANCIAL_INDICATOR_MAP)
        except Exception as exc:
            logger.warning("AkShare 财务指标获取失败 %s: %s", code, exc)
            return {}

    def fetch_balance_sheet(self, symbol) -> dict:
        if not self._is_a_share(symbol):
            return {}
        code = self._fetch_code(symbol)
        try:
            frame = ak.stock_balance_sheet_by_report_em(symbol=code)
            if frame is None or frame.empty:
                return {}
            return _extract_latest_period(frame, BALANCE_SHEET_MAP)
        except Exception as exc:
            logger.warning("AkShare 资产负债表获取失败 %s: %s", code, exc)
            return {}

    def fetch_income_statement(self, symbol) -> dict:
        if not self._is_a_share(symbol):
            return {}
        code = self._fetch_code(symbol)
        try:
            frame = ak.stock_profit_sheet_by_report_em(symbol=code)
            if frame is None or frame.empty:
                return {}
            return _extract_latest_period(frame, INCOME_STATEMENT_MAP)
        except Exception as exc:
            logger.warning("AkShare 利润表获取失败 %s: %s", code, exc)
            return {}

    def fetch_cash_flow(self, symbol) -> dict:
        if not self._is_a_share(symbol):
            return {}
        code = self._fetch_code(symbol)
        try:
            frame = ak.stock_cash_flow_sheet_by_report_em(symbol=code)
            if frame is None or frame.empty:
                return {}
            return _extract_latest_period(frame, CASH_FLOW_MAP)
        except Exception as exc:
            logger.warning("AkShare 现金流量表获取失败 %s: %s", code, exc)
            return {}