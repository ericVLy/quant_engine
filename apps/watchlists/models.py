from django.db import models
from django.conf import settings


class Symbol(models.Model):
    MARKET_CHOICES = [
        ('A', 'A股'),
        ('HK', '港股'),
        ('US', '美股'),
    ]
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    exchange = models.CharField(max_length=20, blank=True)
    market = models.CharField(max_length=10, choices=MARKET_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.code} - {self.name}"


class SymbolNameCache(models.Model):
    """在线数据源的「代码 → 名称」持久化缓存（2026-10-14）。

    为什么需要：``resolve_symbol_name`` 此前每解析**一个**代码都要向在线数据源
    （AkShare）拉取**整个市场**的代码-名称表（A 股约 5400 行）。批量建标的时
    这意味着 N 次全市场请求，慢且对数据源不友好；且结果只存在局部变量里，
    进程一退出就丢。

    设计要点：

    - **整市场批量拉取、按需 TTL**：``fetched_at`` 记录该行数据的获取时间，
      超过 ``SYMBOL_NAME_CACHE_TTL_HOURS`` 才重新拉取（默认 24h，名称极少变动）；
    - **离线可用**：在线源失败时回退到**过期缓存**（stale-while-error），
      宁可用略旧名称，也不让功能整体不可用；
    - 与 ``Symbol`` 表职责分离：``Symbol`` 是业务标的（本项目纳管的），
      本表是**全市场**名称字典（含未纳管标的），供按代码补名称时查。

    放在 ``watchlists`` 而非 ``datasources``：``datasources`` 已依赖
    ``watchlists.models.Symbol``，反向依赖会形成循环导入。
    """

    MARKET_CHOICES = Symbol.MARKET_CHOICES

    market = models.CharField(max_length=10, choices=MARKET_CHOICES, db_index=True)
    # 归一后的代码（大写、去交易所后缀与 sh/sz/bj 前缀），与 Symbol.code 口径一致
    code = models.CharField(max_length=20, db_index=True, verbose_name='归一代码')
    name = models.CharField(max_length=100)
    # 数据来源标识，便于排查某个名称是哪次拉取写进来的
    source = models.CharField(max_length=40, blank=True, verbose_name='数据来源')
    fetched_at = models.DateTimeField(db_index=True, verbose_name='获取时间')

    class Meta:
        db_table = 'watchlists_symbol_name_cache'
        verbose_name = '标的名称缓存'
        verbose_name_plural = '标的名称缓存'
        ordering = ('market', 'code')
        constraints = [
            models.UniqueConstraint(fields=('market', 'code'), name='unique_symbol_name_cache'),
        ]
        indexes = [
            # 按市场查最新获取时间（判定是否需要整市场刷新）
            models.Index(fields=['market', '-fetched_at'], name='idx_name_cache_market_fetched'),
        ]

    def __str__(self):
        return f'{self.market}:{self.code}={self.name}'

class Group(models.Model):
    name = models.CharField(max_length=50, unique=True)
    symbols = models.ManyToManyField(Symbol, related_name='groups', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

class Watchlist(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='watchlists')
    groups = models.ManyToManyField(Group, related_name='watchlists', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} 的自选池"
