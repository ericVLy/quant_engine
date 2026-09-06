"""基本面数据适配器。

第一版选择 AkShare 的 ``stock_individual_info_em`` 作为 A 股基础面来源。
适配器只负责拉取和规范化，不让第三方字段名泄漏到 Case 执行层。
"""

import logging
import re
from datetime import datetime

import akshare as ak

logger = logging.getLogger(__name__)


FIELD_MAP = {
    '股票代码': 'symbol',
    '股票简称': 'name',
    '总股本': 'shares_outstanding',
    '流通股': 'shares_float',
    '总市值': 'market_cap',
    '流通市值': 'float_market_cap',
    '行业': 'industry',
    '上市时间': 'listing_date',
}


def normalize_stock_code(code):
    """将 Symbol 编码转换成 AkShare 接受的六位 A 股代码。"""
    value = str(code or '').strip().upper()
    value = re.sub(r'^(SH|SZ|BJ)', '', value)
    value = re.sub(r'\.(XSHG|XSHE|SH|SZ)$', '', value)
    return value.zfill(6) if value.isdigit() else value


def _normalize_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        numeric = text.replace(',', '')
        try:
            return float(numeric)
        except ValueError:
            return text
    return value.item() if hasattr(value, 'item') else value


class AkshareFundamentalsProvider:
    """从 AkShare 获取 A 股个股基础面信息。"""

    name = 'akshare'

    def fetch(self, symbol):
        if symbol is None or str(getattr(symbol, 'market', '')).upper() != 'A':
            return {}

        code = normalize_stock_code(getattr(symbol, 'code', symbol))
        try:
            frame = ak.stock_individual_info_em(symbol=code)
            if frame is None or frame.empty:
                return {}
            metrics = {}
            for row in frame.itertuples(index=False):
                item = str(getattr(row, 'item', row[0])).strip()
                value = row[1] if len(row) > 1 else None
                field = FIELD_MAP.get(item)
                if field:
                    metrics[field] = _normalize_value(value)
            return metrics
        except Exception as exc:  # 外部数据源不可用时不阻断策略执行
            logger.warning('AkShare 基本面数据获取失败 %s: %s', code, exc)
            return {}

    def context(self, symbol):
        return {
            'provider': self.name,
            'symbol': normalize_stock_code(getattr(symbol, 'code', symbol)),
            'asof': datetime.now().isoformat(),
            'metrics': self.fetch(symbol),
        }