"""按 gm 账户查询同步账户资金（运维/人工核对用）。

调度器内置周期同步（``run_scheduler --funds-source gm``），本命令用于：
首次配置、核对数据、排查资金不一致。

用法：
    # 同步"已发布 Plan 引用"的全部账户
    .venv/bin/python manage.py sync_account_funds

    # 同步指定账户
    .venv/bin/python manage.py sync_account_funds --account-id <gm账户ID>

**失败语义**：任一账户查询失败时**不会**把资金写 0，而是保留上一次成功同步的
数值并以非零退出码结束（见 ``apps.execution.fund_sync``）。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
import json

from django.core.management.base import BaseCommand, CommandError

from apps.execution.fund_sync import (
    FundSyncError, mask_account, sync_account_funds, sync_published_plan_accounts,
)


class Command(BaseCommand):
    help = '按 gm 账户查询同步账户资金（账户总资金 / 可用资金 / 持仓市值）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--account-id', dest='account_id', default='',
            help='指定要同步的交易账户 ID；留空 = 同步已发布 Plan 引用的全部账户',
        )
        parser.add_argument(
            '--source', dest='source', choices=('gm', 'manual'), default='gm',
            help='资金来源标记，写入 AccountFundConfig.source（默认 gm）',
        )
        parser.add_argument(
            '--capital-basis', dest='capital_basis',
            choices=('total', 'cash', 'available'), default='total',
            help='额度上限口径：total（默认，账面资金+持仓市值）/ cash（只看账面资金，'
                 '账户存在本项目未管理的持仓时推荐）/ available（券商可用资金，最保守）',
        )
        parser.add_argument(
            '--show-account-id', dest='show_account_id', action='store_true', default=False,
            help='在输出中显示完整账户 ID（默认脱敏，遵循 N-05 日志/输出卫生）',
        )

    def handle(self, *args, **options):
        from runner.gm_adapter import GmBrokerAdapter

        source = options['source']
        basis = options['capital_basis']
        show_full = options['show_account_id']
        try:
            broker = GmBrokerAdapter()
        except Exception as exc:  # pylint: disable=broad-except
            raise CommandError(f'初始化 gm 账户查询通道失败: {exc}') from exc

        def run_one(account_id):
            return sync_account_funds(
                account_id, broker, source=source, capital_basis=basis)

        if options['account_id']:
            targets = [options['account_id']]
        else:
            targets = None

        failures = 0
        if targets is None:
            results = sync_published_plan_accounts(
                broker, source=source, capital_basis=basis)
            if not results:
                self.stdout.write('没有已发布 Plan 绑定账户，无需同步'
                                  '（可用 --account-id 指定账户）')
                return
        else:
            results = []
            for account_id in targets:
                try:
                    results.append(run_one(account_id))
                except FundSyncError as exc:
                    failures += 1
                    self.stderr.write(self.style.ERROR(
                        f'账户 {self._label(account_id, show_full)} 同步失败: {exc}'))
                    results.append({'account_id': mask_account(account_id), 'error': str(exc)})

        for item in results:
            label = self._label(item.get('account_id', ''), show_full)
            if 'error' in item:
                self.stderr.write(self.style.ERROR(
                    f'  {label} 失败：{item["error"]}（保留上次同步值）'))
                continue
            self.stdout.write(
                f'  {label} 额度={item["total_capital"]}（口径 {item["capital_basis"]}，'
                f'口径值 {item["computed_capital"]}，已分配 {item["allocated_capital"]}'
                f'{"，下限托底" if item.get("clamped") else ""}）'
                f' 总资产={item["total_assets"]} 账面={item["balance"]} '
                f'可用={item["available_cash"]} 持仓市值={item["market_value"]} '
                f'冻结={item["frozen_cash"]} 币种={item.get("currency") or "-"} '
                f'同步时间={item["synced_at"]}'
            )
        self.stdout.write(json.dumps({
            'synced': sum(1 for item in results if 'error' not in item),
            'failed': failures or sum(1 for item in results if 'error' in item),
        }, ensure_ascii=False))
        if any('error' in item for item in results):
            raise CommandError('部分账户资金同步失败（已保留各自上次同步值）')

    @staticmethod
    def _label(account_id, show_full):
        return account_id if show_full else mask_account(account_id)
