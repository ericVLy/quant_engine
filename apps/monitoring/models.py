"""分时监控（模块9）数据模型。

设计约束（见 documents.md 模块9）：
- 分时数据为**临时数据**：开盘记录 → 收盘后由 clear_intraday 清空全表；
- ``ts`` 统一存 UTC（USE_TZ=True），查询/展示层按市场时区转换；
- ``(symbol, ts)`` 唯一：同一分钟重复采样由 ``update_or_create`` 覆盖。
"""
from django.db import models

from apps.watchlists.models import Symbol


class IntradayPoint(models.Model):
    """分时监控点（临时数据：当日有效，收盘后由 clear_intraday 清空）。"""

    symbol = models.ForeignKey(
        Symbol, on_delete=models.CASCADE, related_name='intraday_points',
        verbose_name='标的',
    )
    ts = models.DateTimeField(db_index=True, verbose_name='采样时间(UTC)')
    price = models.DecimalField(max_digits=12, decimal_places=4, verbose_name='现价')
    change = models.DecimalField(max_digits=8, decimal_places=4, verbose_name='涨跌幅%')
    volume = models.BigIntegerField(default=0, verbose_name='累计成交量')
    amount = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True, verbose_name='累计成交额',
    )
    avg_price = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True, verbose_name='均价',
    )
    high = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True, verbose_name='日内最高',
    )
    low = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True, verbose_name='日内最低',
    )
    open_price = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True, verbose_name='开盘价',
    )
    pre_close = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True, verbose_name='昨收价',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '分时监控点'
        verbose_name_plural = '分时监控点'
        ordering = ('symbol', 'ts')
        indexes = [
            models.Index(fields=['symbol', '-ts'], name='idx_intraday_symbol_ts'),
        ]
        constraints = [
            models.UniqueConstraint(fields=('symbol', 'ts'), name='uniq_intraday_symbol_ts'),
        ]

    def __str__(self):
        return f'{self.symbol.code} @ {self.ts:%H:%M} {self.price}'