"""刷新「代码 → 名称」持久化缓存（在线数据源 → ``SymbolNameCache``）。

``resolve_symbol_name`` 在缓存缺失/过期时会**自动**整市场刷新一次，日常使用
通常无需手动执行本命令。以下场景才需要显式刷新：

- 预热：部署后先跑一次，避免首个用户请求时卡在全市场拉取上；
- 定时：纳入 crontab / CI 定时任务，保持缓存新鲜；
- 排障：怀疑名称过期或在线源返回异常时，强制重建。

用法::

    # 刷新全部市场
    .venv/bin/python manage.py refresh_symbol_names

    # 只刷新 A 股
    .venv/bin/python manage.py refresh_symbol_names --market A

    # 查看缓存现状（不联网）
    .venv/bin/python manage.py refresh_symbol_names --stats

**失败语义**：某市场在线源异常时，该市场保持原有缓存不变（不写空、不清空），
其余市场继续刷新；全部失败以非零退出码结束。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max
from django.utils import timezone

# 以模块方式引用服务：直接 ``from services import func`` 会在导入期绑定函数对象，
# 测试替身与后续热替换都无法生效（曾导致命令在测试里绕过 patch 打到真实网络）。
from apps.watchlists import services as name_service

MARKETS = ('A', 'HK', 'US')


class Command(BaseCommand):
    help = '刷新标的名称缓存（在线数据源 → 持久化缓存表）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--market', choices=[*MARKETS, 'all'], default='all',
            help='要刷新的市场；默认 all。',
        )
        parser.add_argument(
            '--stats', action='store_true',
            help='只打印当前缓存统计，不联网刷新。',
        )

    def handle(self, *args, **options):
        # pylint: disable=unused-argument  # BaseCommand 签名
        if options['stats']:
            self._print_stats()
            return

        markets = MARKETS if options['market'] == 'all' else (options['market'],)
        failures = []
        for market in markets:
            self.stdout.write(f'正在刷新 {market} 市场名称缓存…')
            try:
                name_map, source = name_service.fetch_market_name_map(market)
                if not name_map:
                    self.stderr.write(self.style.WARNING(f'  {market}: 在线源无数据，保持原缓存'))
                    failures.append(market)
                    continue
                written = name_service.refresh_symbol_name_cache(market, name_map, source)
                self.stdout.write(self.style.SUCCESS(f'  {market}: 写入 {written} 条（来源 {source}）'))
            except Exception as exc:  # pylint: disable=broad-except
                # 单市场失败不影响其他市场，且不清空已有缓存
                self.stderr.write(self.style.ERROR(f'  {market}: 刷新失败 {exc}（保持原缓存）'))
                failures.append(market)

        if failures:
            raise CommandError(f'以下市场刷新失败: {", ".join(failures)}')

    def _print_stats(self):
        from apps.watchlists.models import SymbolNameCache

        rows = (SymbolNameCache.objects
                .values('market', 'source')
                .annotate(count=Count('id'), latest=Max('fetched_at'))
                .order_by('market'))
        if not rows:
            self.stdout.write('名称缓存为空（首次解析时会自动拉取）')
            return
        self.stdout.write(f'缓存有效期: {_ttl_description()}')
        for row in rows:
            latest = row['latest']
            age = ''
            if latest is not None:
                hours = (timezone.now() - latest).total_seconds() / 3600
                age = f'，最近更新 {hours:.1f} 小时前'
            self.stdout.write(
                f"  {row['market']}: {row['count']} 条"
                f"（来源 {row['source'] or '-'}）{age}",
            )


def _ttl_description():
    ttl = name_service._cache_ttl_hours()  # pylint: disable=protected-access  # 内部函数，避免把 TTL 细节暴露为公开 API
    return '不过期' if ttl <= 0 else f'{ttl} 小时'
