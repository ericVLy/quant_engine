import re
from django.conf import settings
from django.db import models, connections, transaction
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


def normalize_symbol_code_for_table(symbol_code):
    """将交易代码转换为数据库表名中的安全片段。"""
    cleaned = re.sub(r'[^A-Za-z0-9]+', '_', str(symbol_code)).strip('_')
    return cleaned.lower() if cleaned else 'unknown'


def get_kline_database_alias():
    """返回 K 线数据独立数据库别名。默认使用专用 kline alias。"""
    return getattr(settings, 'KLINE_DB_ALIAS', 'kline')


_RUNTIME_KLINE_MODELS = {}


def get_runtime_kline_model(symbol):
    """根据 symbol 生成一个运行时 K 线模型，并确保该分表存在。"""
    table_name = get_kline_table_name(symbol)
    if table_name in _RUNTIME_KLINE_MODELS:
        return _RUNTIME_KLINE_MODELS[table_name]

    db_alias = get_kline_database_alias()
    db = connections[db_alias]
    model = _build_runtime_kline_model(symbol, table_name)

    if table_name not in db.introspection.table_names():
        if db.vendor == 'sqlite':
            field_sql = [
                'id INTEGER PRIMARY KEY AUTOINCREMENT',
                'symbol_id BIGINT NOT NULL',
                'date DATE NOT NULL',
                'open DECIMAL(12,4) NOT NULL',
                'high DECIMAL(12,4) NOT NULL',
                'low DECIMAL(12,4) NOT NULL',
                'close DECIMAL(12,4) NOT NULL',
                'volume BIGINT NOT NULL',
                'amount DECIMAL(20,2)',
                'created_at DATETIME NOT NULL',
                'updated_at DATETIME NOT NULL',
            ]
        else:
            field_sql = [
                'id BIGINT PRIMARY KEY AUTO_INCREMENT',
                'symbol_id BIGINT NOT NULL',
                'date DATE NOT NULL',
                'open DECIMAL(12,4) NOT NULL',
                'high DECIMAL(12,4) NOT NULL',
                'low DECIMAL(12,4) NOT NULL',
                'close DECIMAL(12,4) NOT NULL',
                'volume BIGINT NOT NULL',
                'amount DECIMAL(20,2)',
                'created_at DATETIME NOT NULL',
                'updated_at DATETIME NOT NULL',
            ]

        if str(symbol.market).upper() == 'A':
            field_sql += [
                'adj_factor DECIMAL(12,6) NOT NULL DEFAULT 1.0',
                'limit_up DECIMAL(12,4)',
                'limit_down DECIMAL(12,4)',
                'turnover_rate DECIMAL(8,4)',
            ]
        elif str(symbol.market).upper() == 'HK':
            field_sql += [
                'prev_close DECIMAL(12,4)',
                'currency VARCHAR(10) NOT NULL DEFAULT "HKD"',
            ]
        elif str(symbol.market).upper() == 'US':
            field_sql += [
                'split_factor DECIMAL(12,6) NOT NULL DEFAULT 1.0',
                'pre_market_price DECIMAL(12,4)',
                'after_hours_price DECIMAL(12,4)',
            ]
        field_sql.append('UNIQUE(symbol_id, date)')

        if db.vendor != 'sqlite':
            try:
                transaction.set_autocommit(True, using=db_alias)
            except Exception:
                pass

        try:
            with db.cursor() as cursor:
                cursor.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(field_sql)})")
                cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_symbol_date ON {table_name}(symbol_id, date)")
        finally:
            if db.vendor != 'sqlite':
                try:
                    transaction.set_autocommit(False, using=db_alias)
                except Exception:
                    pass

    _RUNTIME_KLINE_MODELS[table_name] = model
    return model


def _build_runtime_kline_model(symbol, table_name):
    """创建动态模型，用于对单个 symbol 的分表执行 ORM 操作。"""
    market = str(symbol.market).upper()

    attrs = {
        '__module__': __name__,
        'Meta': type('Meta', (), {
            'managed': False,
            'db_table': table_name,
            'app_label': 'datasources',
            'ordering': ['date'],
            'unique_together': [('symbol_id', 'date')],
        }),
        'symbol_id': models.BigIntegerField(db_index=True),
        'date': models.DateField(db_index=True),
        'open': models.DecimalField(max_digits=12, decimal_places=4),
        'high': models.DecimalField(max_digits=12, decimal_places=4),
        'low': models.DecimalField(max_digits=12, decimal_places=4),
        'close': models.DecimalField(max_digits=12, decimal_places=4),
        'volume': models.BigIntegerField(),
        'amount': models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True),
        'created_at': models.DateTimeField(auto_now_add=True),
        'updated_at': models.DateTimeField(auto_now=True),
    }

    if market == 'A':
        attrs.update({
            'adj_factor': models.DecimalField(max_digits=12, decimal_places=6, default=1.0),
            'limit_up': models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True),
            'limit_down': models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True),
            'turnover_rate': models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True),
        })
    elif market == 'HK':
        attrs.update({
            'prev_close': models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True),
            'currency': models.CharField(max_length=10, default='HKD'),
        })
    elif market == 'US':
        attrs.update({
            'split_factor': models.DecimalField(max_digits=12, decimal_places=6, default=1.0),
            'pre_market_price': models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True),
            'after_hours_price': models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True),
        })

    class_name = f"Runtime{market}{normalize_symbol_code_for_table(symbol.code).title()}KLine"
    return type(class_name, (models.Model,), attrs)


def get_kline_table_name(symbol):
    """按市场 + 股票编码生成分表名，示例：kline_a_000001。"""
    market_key = {
        'A': 'a',
        'HK': 'hk',
        'US': 'us',
    }.get(str(symbol.market).upper(), str(symbol.market).lower())
    return f'kline_{market_key}_{normalize_symbol_code_for_table(symbol.code)}'


def ensure_kline_table(symbol):
    """保证 symbol 对应的运行时 K 线表存在。使用动态模型 + schema_editor，避免直接拼接 SQL。"""
    model = get_runtime_kline_model(symbol)
    return model._meta.db_table
