"""基本面数据缓存缓存 Provider：通过持久化快照（asof 语义）包裹任意 FundamentalsProvider。

在保证「指定历史时点不读取未来数据」的前提下，命中有效缓存即返回，过期/未命中回源刷新。
"""
from datetime import timedelta

from django.utils import timezone

from apps.datasources.models import FundamentalCacheMeta, FundamentalSnapshot

from .fundamentals import FundamentalsProvider


class CachedFundamentalsProvider(FundamentalsProvider):
    """缓存装饰 Provider。

    参数
    ----
    provider : FundamentalsProvider
        被包裹的真实数据源。
    ttl : timedelta
        缓存有效期。默认 24 小时。
    历史时点查询通过 ``context(symbol, asof=...)`` 传递，只读取
    ``asof <= 目标时点`` 的最新一条快照。
    """

    name = 'cached'

    def __init__(self, provider, ttl=None):
        if not isinstance(provider, FundamentalsProvider):
            raise TypeError('provider 必须是 FundamentalsProvider 实例')
        self._provider = provider
        self.ttl = ttl or timedelta(hours=24)

    # 子报表能力透传
    @property
    def supports_financial_indicators(self):
        return self._provider.supports_financial_indicators

    @property
    def supports_balance_sheet(self):
        return self._provider.supports_balance_sheet

    @property
    def supports_income_statement(self):
        return self._provider.supports_income_statement

    @property
    def supports_cash_flow(self):
        return self._provider.supports_cash_flow

    def _resolve_symbol(self, symbol):
        from apps.watchlists.models import Symbol
        if isinstance(symbol, Symbol):
            return symbol
        return Symbol.objects.filter(code=symbol).first()

    def _latest_cached(self, symbol_obj, asof=None):
        """读取 asof <= 目标时点的最新快照，返回 (snapshot, is_fresh)。

        历史时点查询 (asof 已给定) 是点读，不存在“新鲜度”概念，直接返回命中；
        仅未指定 asof 的实时路径才应用 TTL 过期判定。
        """
        now = timezone.now()
        query = FundamentalSnapshot.objects.filter(symbol=symbol_obj)
        if asof is not None:
            query = query.filter(asof__lte=asof)
        snap = query.order_by('-asof').first()
        if snap is None:
            return None, False
        if asof is not None:
            # 历史点读：命中即有效，不判 TTL
            return snap, True
        fresh = (now - snap.asof) <= self.ttl
        return snap, fresh

    def fetch(self, symbol, asof=None) -> dict:
        """命中有效缓存直接返回；否则回源并回填缓存。"""
        symbol_obj = self._resolve_symbol(symbol)
        if symbol_obj is None:
            return self._provider.fetch(symbol)

        now = timezone.now()
        snap, fresh = self._latest_cached(symbol_obj, asof=asof)
        if snap is not None and fresh:
            self._bump_meta(symbol_obj, hit=True)
            return dict(snap.payload or {})

        # 未命中（或过期）→ 回源
        self._bump_meta(symbol_obj, hit=False)
        metrics = self._provider.fetch(symbol_obj)
        if not metrics:
            # 回源失败但已有旧缓存 → 降级返回旧缓存（防抖）
            if snap is not None:
                return dict(snap.payload or {})
            return {}

        # 回填缓存
        FundamentalSnapshot.objects.create(
            symbol=symbol_obj,
            asof=asof if asof is not None else now,
            payload=metrics,
        )
        return metrics

    def _bump_meta(self, symbol_obj, hit):
        meta, _ = FundamentalCacheMeta.objects.get_or_create(symbol=symbol_obj)
        if hit:
            meta.hit_count += 1
        else:
            meta.miss_count += 1
        meta.last_synced_at = timezone.now()
        meta.save(update_fields=['hit_count', 'miss_count', 'last_synced_at', 'updated_at'])

    def fetch_basic_info(self, symbol) -> dict:
        return self.fetch(symbol)

    def fetch_financial_indicators(self, symbol) -> dict:
        return self._provider.fetch_financial_indicators(symbol) if self._resolve_symbol(symbol) else {}

    def fetch_balance_sheet(self, symbol) -> dict:
        return self._provider.fetch_balance_sheet(symbol) if self._resolve_symbol(symbol) else {}

    def fetch_income_statement(self, symbol) -> dict:
        return self._provider.fetch_income_statement(symbol) if self._resolve_symbol(symbol) else {}

    def fetch_cash_flow(self, symbol) -> dict:
        return self._provider.fetch_cash_flow(symbol) if self._resolve_symbol(symbol) else {}

    def context(self, symbol, asof=None) -> dict:
        """返回统一上下文，支持历史时点 (asof) 查询。"""
        return {
            'provider': f'cached:{self._provider.name}',
            'symbol': getattr(symbol, 'code', None) or str(symbol),
            'asof': (asof or timezone.now()).isoformat(),
            'metrics': self.fetch(symbol, asof=asof),
        }