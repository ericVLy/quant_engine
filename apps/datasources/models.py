from django.db import models
from apps.watchlists.models import Symbol

# ============================================================
# 1. 数据源配置（第三方数据源连接信息）
# ============================================================
class DataSource(models.Model):
    SOURCE_TYPE_CHOICES = [
        ('akshare', 'AkShare'),
        ('tushare', 'TuShare'),
        ('tdx', 'TDX'),
        ('yfinance', 'YFinance'),
    ]
    name = models.CharField(max_length=50)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES)
    endpoint = models.URLField(blank=True, null=True)
    auth_info = models.JSONField(default=dict, blank=True)
    priority = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

# ============================================================
# 2. 实时快照缓存（仅保留最新值，用于盘中快速查询）
# ============================================================
class RealtimeSnapshot(models.Model):
    symbol = models.OneToOneField(Symbol, on_delete=models.CASCADE, primary_key=True, related_name='snapshot')
    price = models.DecimalField(max_digits=12, decimal_places=4, verbose_name="最新价")
    change = models.DecimalField(max_digits=8, decimal_places=4, verbose_name="涨跌幅%")
    volume = models.BigIntegerField(verbose_name="成交量")
    turnover = models.DecimalField(max_digits=20, decimal_places=2, verbose_name="成交额")
    high = models.DecimalField(max_digits=12, decimal_places=4, verbose_name="日内最高")
    low = models.DecimalField(max_digits=12, decimal_places=4, verbose_name="日内最低")
    open_price = models.DecimalField(max_digits=12, decimal_places=4, verbose_name="开盘价")
    pre_close = models.DecimalField(max_digits=12, decimal_places=4, verbose_name="昨收价")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.symbol.code} @ {self.price} ({self.change}%)"

# ============================================================
# 3. K线数据（抽象基类 + 各市场子类）
# ============================================================
class AbstractKLine(models.Model):
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name="%(class)s_records")
    date = models.DateField(db_index=True)
    open = models.DecimalField(max_digits=12, decimal_places=4)
    high = models.DecimalField(max_digits=12, decimal_places=4)
    low = models.DecimalField(max_digits=12, decimal_places=4)
    close = models.DecimalField(max_digits=12, decimal_places=4)
    volume = models.BigIntegerField(verbose_name="成交量（股数）")
    amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True, verbose_name="成交额（元）")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['date']
        indexes = [
            models.Index(fields=['symbol', 'date']),
        ]

    def __str__(self):
        return f"{self.symbol.code} {self.date} O:{self.open} C:{self.close}"

class AStockKLine(AbstractKLine):
    """A股 K线"""
    adj_factor = models.DecimalField(max_digits=12, decimal_places=6, default=1.0, verbose_name="复权因子")
    limit_up = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, verbose_name="涨停价")
    limit_down = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, verbose_name="跌停价")
    turnover_rate = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True, verbose_name="换手率%")

    class Meta:
        db_table = 'kline_a_stock'
        verbose_name = 'A股K线'
        verbose_name_plural = 'A股K线'
        unique_together = [['symbol', 'date']]

class HKStockKLine(AbstractKLine):
    """港股 K线"""
    prev_close = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, verbose_name="前收盘价")
    currency = models.CharField(max_length=10, default='HKD', verbose_name="货币单位")

    class Meta:
        db_table = 'kline_hk_stock'
        verbose_name = '港股K线'
        verbose_name_plural = '港股K线'
        unique_together = [['symbol', 'date']]

class USStockKLine(AbstractKLine):
    """美股 K线"""
    split_factor = models.DecimalField(max_digits=12, decimal_places=6, default=1.0, verbose_name="拆分因子")
    pre_market_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, verbose_name="盘前价")
    after_hours_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, verbose_name="盘后价")

    class Meta:
        db_table = 'kline_us_stock'
        verbose_name = '美股K线'
        verbose_name_plural = '美股K线'
        unique_together = [['symbol', 'date']]

# ============================================================
# 4. K线数据同步日志（记录每次拉取的状态）
# ============================================================
class KLineSyncLog(models.Model):
    SYNC_TYPE_CHOICES = [
        ('daily', '日线'),
        ('minute', '分钟线'),
    ]
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='sync_logs')
    sync_type = models.CharField(max_length=20, choices=SYNC_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    records_added = models.PositiveIntegerField(default=0)
    records_skipped = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=[('success','成功'),('failed','失败'),('partial','部分成功')])
    error_msg = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.symbol.code} {self.sync_type} {self.start_date}~{self.end_date}"
