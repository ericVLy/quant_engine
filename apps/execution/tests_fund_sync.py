"""账户资金同步（``apps.execution.fund_sync``）专项测试。

重点覆盖 gm ``Cash`` 字段归一、**失败绝不写 0** 的安全约定、TTL 刷新守卫、
批量同步的失败隔离，以及与执行服务 / gm 适配器 / 管理命令的接线。
"""
# pylint: disable=import-outside-toplevel,protected-access  # 延迟导入以规避循环依赖/加载期副作用；测试需访问私有成员以验证内部状态
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.execution.fund_sync import (
    FundSyncError, ensure_funds_fresh, mask_account, normalize_cash,
    normalize_positions, sync_account_funds, sync_published_plan_accounts,
)
from apps.execution.models import AccountFundConfig
from apps.plans.models import Plan
from apps.suites.models import Suite

ACCOUNT = 'efd94fdb-a020-11f1-b1d8-00163e022aa6'
FULL_CASH = {
    'account_id': ACCOUNT,
    'balance': 80000,
    'market_value': 20000,
    'available': 75000,
    'frozen': 3000,
    'order_frozen': 2000,
    'currency': 'CNY',
}


class _FakeBroker:
    """最小账户查询替身。"""

    def __init__(self, cash=None, error=None):
        self.cash = {} if cash is None else cash
        self.error = error
        self.calls = 0

    def get_account(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.cash

    def get_positions(self):
        return []


class NormalizeCashTest(TestCase):
    """gm ``Cash`` dict → :class:`AccountSnapshot`。"""

    def test_full_payload_totals_are_derived(self):
        snap = normalize_cash(FULL_CASH, ACCOUNT)
        self.assertEqual(snap.total_assets, Decimal('100000'))     # 80000 + 20000
        self.assertEqual(snap.available_cash, Decimal('75000'))
        self.assertEqual(snap.market_value, Decimal('20000'))
        self.assertEqual(snap.frozen_cash, Decimal('5000'))         # 3000 + 2000
        self.assertEqual(snap.currency, 'CNY')
        self.assertEqual(snap.account_id, ACCOUNT)

    def test_empty_payload_yields_no_total(self):
        for raw in ({}, None, 'oops', 123):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_cash(raw, ACCOUNT).total_assets)

    def test_nav_fallback_for_fund_accounts(self):
        snap = normalize_cash({'nav': 12345.6, 'available': 9000}, ACCOUNT)
        self.assertEqual(snap.total_assets, Decimal('12345.6'))

    def test_missing_market_value_treated_as_zero(self):
        snap = normalize_cash({'balance': 5000}, ACCOUNT)
        self.assertEqual(snap.total_assets, Decimal('5000'))
        self.assertIsNone(snap.market_value)
        self.assertIsNone(snap.frozen_cash)

    def test_non_numeric_and_bool_values_ignored(self):
        snap = normalize_cash(
            {'balance': 'abc', 'available': True, 'market_value': '2000.50'}, ACCOUNT)
        self.assertEqual(snap.total_assets, Decimal('2000.50'))
        self.assertIsNone(snap.available_cash)

    def test_cash_key_used_as_available_fallback(self):
        self.assertEqual(normalize_cash({'cash': 777}, ACCOUNT).available_cash, Decimal('777'))

    def test_as_dict_is_json_safe(self):
        data = normalize_cash(FULL_CASH, ACCOUNT).as_dict()
        self.assertEqual(data['total_assets'], '100000')
        self.assertIsInstance(data['available_cash'], str)


class MaskAccountTest(TestCase):
    """N-05 日志卫生：账户 ID 不进明文。"""

    def test_long_account_is_masked(self):
        masked = mask_account(ACCOUNT)
        self.assertNotIn(ACCOUNT, masked)
        self.assertTrue(masked.startswith('efd9'))

    def test_short_and_empty_values(self):
        self.assertEqual(mask_account('12345678'), '12345678')
        self.assertEqual(mask_account(''), '(空)')

    def test_sync_log_masks_account_id(self):
        with self.assertLogs('apps.execution.fund_sync', level='INFO') as captured:
            sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        joined = '\n'.join(captured.output)
        self.assertNotIn(ACCOUNT, joined)
        self.assertIn('efd9', joined)


class CapitalBasisTest(TestCase):
    """额度口径：外部持仓市值盘中波动不应扰动"可部署额度"。"""

    def _sync(self, cash, basis='total', allocated=None):
        if allocated is not None:
            suite = Suite.objects.create(name=f'S{allocated}')
            Plan.objects.create(
                name=f'P{allocated}', root_suite=suite, status='published',
                trigger_type='manual', account_id=ACCOUNT,
                allocated_capital=Decimal(str(allocated)))
        return sync_account_funds(ACCOUNT, _FakeBroker(cash), capital_basis=basis)

    def test_total_basis_uses_balance_plus_market_value(self):
        result = self._sync(FULL_CASH, basis='total')
        self.assertEqual(result['total_capital'], '100000.00')   # 已量化到 2 位
        self.assertEqual(result['capital_basis'], 'total')
        self.assertFalse(result['clamped'])

    def test_cash_basis_ignores_market_value(self):
        result = self._sync(FULL_CASH, basis='cash')
        self.assertEqual(result['total_capital'], '80000.00')    # 只取 balance
        self.assertEqual(result['capital_basis'], 'cash')
        self.assertEqual(result['market_value'], '20000')        # 仍记录持仓市值
        self.assertEqual(
            AccountFundConfig.objects.get(account_id=ACCOUNT).capital_basis, 'cash')

    def test_available_basis_uses_broker_available(self):
        self.assertEqual(
            self._sync(FULL_CASH, basis='available')['total_capital'], '75000.00')

    def test_basis_without_field_falls_back_to_total(self):
        """口径字段缺失时回退到总资产口径，并在结果中标明实际口径。"""
        result = self._sync({'market_value': 20000}, basis='cash')
        self.assertEqual(result['capital_basis'], 'total')
        self.assertEqual(result['total_capital'], '20000.00')

    def test_invalid_basis_rejected(self):
        with self.assertRaises(FundSyncError):
            self._sync(FULL_CASH, basis='nav')

    def test_capital_never_drops_below_allocated_amounts(self):
        """核心不变式：口径值跌破已分配额度时下限托底，保留既有额度分配。"""
        result = self._sync({'balance': 1000, 'market_value': 0}, basis='cash',
                            allocated=50000)
        self.assertEqual(result['computed_capital'], '1000')
        self.assertEqual(result['allocated_capital'], '50000')
        self.assertEqual(result['total_capital'], '50000.00')   # 托底
        self.assertTrue(result['clamped'])
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.total_capital, Decimal('50000'))
        self.assertEqual(cfg.available_capital, Decimal('0'))

    def test_invariant_does_not_inflate_above_computed(self):
        result = self._sync(FULL_CASH, basis='cash', allocated=1000)
        self.assertEqual(result['total_capital'], '80000.00')
        self.assertFalse(result['clamped'])

    def test_clamped_sync_is_logged(self):
        with self.assertLogs('apps.execution.fund_sync', level='INFO') as captured:
            self._sync({'balance': 10}, basis='cash', allocated=5000)
        self.assertIn('下限托底', '\n'.join(captured.output))

    def test_subcent_precision_is_not_reported_as_clamped(self):
        """gm 返回值普遍带小数位：量化四舍五入不得被误报为"下限托底"。"""
        result = self._sync({'balance': 997655.9999847412}, basis='cash')
        self.assertEqual(result['total_capital'], '997656.00')   # 已量化到分
        self.assertEqual(result['computed_capital'], '997655.9999847412')
        self.assertFalse(result['clamped'])                      # 但并未被托底

    def test_snapshot_capital_by_basis(self):
        snap = normalize_cash(FULL_CASH, ACCOUNT)
        self.assertEqual(snap.capital_by_basis('total'), Decimal('100000'))
        self.assertEqual(snap.capital_by_basis('cash'), Decimal('80000'))
        self.assertEqual(snap.capital_by_basis('available'), Decimal('75000'))
        self.assertIsNone(normalize_cash({'nav': 5}, 'x').capital_by_basis('cash'))
        self.assertEqual(normalize_cash({'nav': 5}, 'x').capital_by_basis('total'),
                         Decimal('5'))

    def test_service_forwards_capital_basis(self):
        from runner.service import PlanExecutionService

        service = PlanExecutionService(
            suite_runner=object(), funds_broker=_FakeBroker(FULL_CASH),
            funds_ttl=0, funds_capital_basis='cash')
        self.assertEqual(service.funds_capital_basis, 'cash')
        suite = Suite.objects.create(name='S')
        plan = Plan.objects.create(
            name='P', root_suite=suite, status='published', trigger_type='manual',
            account_id=ACCOUNT)
        service._refresh_funds(plan)
        self.assertEqual(
            AccountFundConfig.objects.get(account_id=ACCOUNT).total_capital,
            Decimal('80000'))                                  # cash 口径


class _BindableBroker(_FakeBroker):
    """记录 ``set_account_id`` 调用的账户查询替身。"""

    def __init__(self, cash=None, error=None, bind_error=None):
        super().__init__(cash=cash, error=error)
        self.bind_error = bind_error
        self.bound = []
        self.account_id = None

    def set_account_id(self, account_id):
        self.bound.append(account_id)
        if self.bind_error is not None:
            raise self.bind_error
        self.account_id = account_id
        return account_id


class BindAccountTest(TestCase):
    """gm 要求先绑定账户：未绑定会报 status 1020「无效的ACCOUNT_ID」。"""

    def test_binds_account_before_query(self):
        broker = _BindableBroker(FULL_CASH)
        sync_account_funds(ACCOUNT, broker)
        self.assertEqual(broker.bound, [ACCOUNT])
        self.assertEqual(broker.account_id, ACCOUNT)

    def test_rebinds_when_account_changes(self):
        broker = _BindableBroker(FULL_CASH)
        sync_account_funds('acc-1', broker)
        sync_account_funds('acc-2', broker)
        self.assertEqual(broker.bound, ['acc-1', 'acc-2'])

    def test_skips_rebinding_for_same_account(self):
        broker = _BindableBroker(FULL_CASH)
        sync_account_funds(ACCOUNT, broker)
        sync_account_funds(ACCOUNT, broker)
        self.assertEqual(broker.bound, [ACCOUNT])

    def test_binding_failure_does_not_break_sync(self):
        broker = _BindableBroker(FULL_CASH, bind_error=RuntimeError('nope'))
        result = sync_account_funds(ACCOUNT, broker)   # 仍按未绑定查询
        self.assertEqual(result['total_assets'], '100000')
        self.assertEqual(broker.bound, [ACCOUNT])

    def test_broker_without_setter_still_works(self):
        result = sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))  # 无 set_account_id
        self.assertEqual(result['total_assets'], '100000')


class SyncAccountFundsTest(TestCase):
    """写库与失败语义。"""

    def test_creates_config_row_on_first_sync(self):
        result = sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        self.assertTrue(result['created'])
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.total_capital, Decimal('100000'))
        self.assertEqual(cfg.available_cash, Decimal('75000'))
        self.assertEqual(cfg.source, 'gm')
        self.assertIsNotNone(cfg.synced_at)

    def test_updates_existing_row_in_place(self):
        AccountFundConfig.objects.create(account_id=ACCOUNT, total_capital=Decimal('1'))
        result = sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        self.assertFalse(result['created'])
        self.assertEqual(
            AccountFundConfig.objects.get(account_id=ACCOUNT).total_capital,
            Decimal('100000'))

    def test_overwrites_manual_source_with_gm(self):
        cfg = AccountFundConfig.objects.create(account_id=ACCOUNT, total_capital=Decimal('5'))
        self.assertEqual(cfg.source, 'manual')
        sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        cfg.refresh_from_db()
        self.assertEqual(cfg.source, 'gm')
        self.assertFalse(cfg.is_stale)

    def test_empty_payload_keeps_previous_values(self):
        """核心安全约定：查询无数据不得把资金写 0。"""
        sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        with self.assertRaises(FundSyncError):
            sync_account_funds(ACCOUNT, _FakeBroker({}))
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.total_capital, Decimal('100000'))
        self.assertEqual(cfg.available_cash, Decimal('75000'))

    def test_broker_exception_keeps_previous_values(self):
        sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        with self.assertRaises(FundSyncError):
            sync_account_funds(ACCOUNT, _FakeBroker(error=RuntimeError('terminal down')))
        self.assertEqual(
            AccountFundConfig.objects.get(account_id=ACCOUNT).total_capital,
            Decimal('100000'))

    def test_missing_arguments_rejected(self):
        with self.assertRaises(FundSyncError):
            sync_account_funds('', _FakeBroker(FULL_CASH))
        with self.assertRaises(FundSyncError):
            sync_account_funds(ACCOUNT, None)

    def test_available_capital_still_uses_internal_allocation(self):
        """内部额度 = 总资金 − Plan 占用，与 gm available_cash 相互独立。"""
        suite = Suite.objects.create(name='S')
        Plan.objects.create(
            name='P', root_suite=suite, status='published', trigger_type='manual',
            account_id=ACCOUNT, allocated_capital=Decimal('30000'))
        sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.available_capital, Decimal('70000'))
        self.assertEqual(cfg.available_cash, Decimal('75000'))


class EnsureFundsFreshTest(TestCase):
    """TTL 守卫。"""

    def test_skips_within_ttl(self):
        sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        broker = _FakeBroker(FULL_CASH)
        result = ensure_funds_fresh(ACCOUNT, broker, ttl_seconds=300)
        self.assertFalse(result['synced'])
        self.assertEqual(broker.calls, 0)

    def test_syncs_when_ttl_expired(self):
        sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        AccountFundConfig.objects.filter(pk=cfg.pk).update(
            synced_at=timezone.now() - timedelta(minutes=10))
        broker = _FakeBroker(FULL_CASH)
        self.assertTrue(ensure_funds_fresh(ACCOUNT, broker, ttl_seconds=30)['synced'])
        self.assertEqual(broker.calls, 1)

    def test_non_positive_ttl_always_syncs(self):
        sync_account_funds(ACCOUNT, _FakeBroker(FULL_CASH))
        broker = _FakeBroker(FULL_CASH)
        self.assertTrue(ensure_funds_fresh(ACCOUNT, broker, ttl_seconds=0)['synced'])
        self.assertEqual(broker.calls, 1)

    def test_manual_source_is_refreshed(self):
        """手工维护的数据没有 synced_at，切到 gm 后应立即同步。"""
        AccountFundConfig.objects.create(account_id=ACCOUNT, total_capital=Decimal('9'))
        self.assertTrue(
            ensure_funds_fresh(ACCOUNT, _FakeBroker(FULL_CASH), ttl_seconds=300)['synced'])

    def test_noop_without_account_or_broker(self):
        self.assertFalse(ensure_funds_fresh('', _FakeBroker(FULL_CASH))['synced'])
        self.assertFalse(ensure_funds_fresh(ACCOUNT, None)['synced'])


class SyncPublishedPlanAccountsTest(TestCase):
    """批量同步。"""

    def _plan(self, name, account_id):
        suite = Suite.objects.create(name=name)
        return Plan.objects.create(
            name=name, root_suite=suite, status='published', trigger_type='manual',
            account_id=account_id)

    def test_syncs_each_distinct_account_once(self):
        self._plan('P1', 'acc-1')
        self._plan('P2', 'acc-1')
        self._plan('P3', 'acc-2')
        self.assertEqual(len(sync_published_plan_accounts(_FakeBroker(FULL_CASH))), 2)
        self.assertEqual(AccountFundConfig.objects.filter(source='gm').count(), 2)

    def test_one_failing_account_is_isolated(self):
        self._plan('P1', 'acc-ok')
        self._plan('P2', 'acc-empty')
        results = sync_published_plan_accounts(_FakeBroker({}))
        self.assertEqual(len(results), 2)
        self.assertTrue(all('error' in item for item in results))
        self.assertFalse(AccountFundConfig.objects.exists())   # 未写入 0

    def test_no_published_accounts_returns_empty(self):
        self.assertEqual(sync_published_plan_accounts(_FakeBroker(FULL_CASH)), [])
        self.assertEqual(sync_published_plan_accounts(None), [])


class GmAdapterPositionsTest(TestCase):
    """gm SDK 暴露的是 ``get_position``（单数）——适配器必须能兜住。"""

    def _adapter(self, api):
        from runner.gm_adapter import GmBrokerAdapter

        return GmBrokerAdapter(token='t', api=api, account_id='acc-1')

    class _BaseApi:
        """无副作用的 gm API 桩（屏蔽真实 GM_TOKEN / 终端连接）。"""

        def set_token(self, token):
            return None

        def set_serv_addr(self, addr):
            return None

        def set_account_id(self, account_id):
            return None

    def test_falls_back_to_singular_get_position(self):
        class _Api(GmAdapterPositionsTest._BaseApi):
            def get_position(self, account_id=None):
                return [{'symbol': '000426.SZ', 'volume': 100}]

        self.assertEqual(
            self._adapter(_Api()).get_positions(),
            [{'symbol': '000426.SZ', 'volume': 100}])

    def test_prefers_plural_when_available(self):
        class _Api(GmAdapterPositionsTest._BaseApi):
            def get_positions(self, account_id=None):
                return [{'plural': True}]

        self.assertEqual(self._adapter(_Api()).get_positions(), [{'plural': True}])

    def test_returns_empty_when_sdk_exposes_neither(self):
        self.assertEqual(
            self._adapter(GmAdapterPositionsTest._BaseApi()).get_positions(), [])


class ExecutionServiceFundsWiringTest(TestCase):
    """执行服务接线：执行前 TTL 同步；装配参数。"""

    def setUp(self):
        self.suite = Suite.objects.create(name='S')
        self.plan = Plan.objects.create(
            name='P', root_suite=self.suite, status='published', trigger_type='manual',
            account_id=ACCOUNT)

    def test_refresh_funds_is_noop_without_broker(self):
        from runner.service import PlanExecutionService

        PlanExecutionService(suite_runner=object())._refresh_funds(self.plan)
        self.assertFalse(AccountFundConfig.objects.exists())

    def test_refresh_funds_syncs_before_execution(self):
        from runner.service import PlanExecutionService

        broker = _FakeBroker(FULL_CASH)
        PlanExecutionService(
            suite_runner=object(), funds_broker=broker, funds_ttl=300,
        )._refresh_funds(self.plan)
        self.assertEqual(broker.calls, 1)
        self.assertEqual(
            AccountFundConfig.objects.get(account_id=ACCOUNT).total_capital,
            Decimal('100000'))

    def test_refresh_funds_swallows_errors(self):
        """资金同步失败不阻断执行（只记日志）。"""
        from runner.service import PlanExecutionService

        service = PlanExecutionService(
            suite_runner=object(),
            funds_broker=_FakeBroker(error=RuntimeError('down')),
            funds_ttl=0,
        )
        with self.assertLogs('runner.service', level='WARNING'):
            service._refresh_funds(self.plan)      # 不抛异常
        self.assertFalse(AccountFundConfig.objects.exists())

    def test_build_service_wires_funds_source(self):
        from runner.service import build_execution_service

        sentinel = object()
        with patch('runner.gm_adapter.GmBrokerAdapter', return_value=sentinel):
            service = build_execution_service(order_broker='none', funds_source='gm')
        self.assertIsNone(service.broker)              # 不下单
        self.assertIs(service.funds_broker, sentinel)   # 只同步资金
        self.assertIs(service._suite_runner.risk_controller.account_provider, sentinel)

    def test_build_service_rejects_unknown_funds_source(self):
        from runner.service import build_execution_service

        with self.assertRaisesRegex(ValueError, 'funds_source'):
            build_execution_service(funds_source='ib')

    def test_build_service_defaults_disable_both_channels(self):
        from runner.service import build_execution_service

        service = build_execution_service()
        self.assertIsNone(service.broker)
        self.assertIsNone(service.funds_broker)
        self.assertIsNone(service._suite_runner.risk_controller.account_provider)


class SyncAccountFundsCommandTest(TestCase):
    """``manage.py sync_account_funds``。"""

    def _run(self, **options):
        defaults = {'account_id': '', 'source': 'gm', 'capital_basis': 'total',
                    'show_account_id': False}
        defaults.update(options)
        out, err = StringIO(), StringIO()
        call_command('sync_account_funds', stdout=out, stderr=err, **defaults)
        return out.getvalue(), err.getvalue()

    def test_syncs_published_plan_accounts_masked(self):
        suite = Suite.objects.create(name='S')
        Plan.objects.create(
            name='P', root_suite=suite, status='published', trigger_type='manual',
            account_id=ACCOUNT)
        with patch('runner.gm_adapter.GmBrokerAdapter', return_value=_FakeBroker(FULL_CASH)):
            out, _ = self._run()
        self.assertNotIn(ACCOUNT, out)          # 默认脱敏
        self.assertIn('额度=100000.00', out)
        self.assertIn('总资产=100000', out)
        self.assertIn('口径 total', out)
        self.assertIn('"synced": 1', out)

    def test_explicit_account_id_and_full_display(self):
        with patch('runner.gm_adapter.GmBrokerAdapter', return_value=_FakeBroker(FULL_CASH)):
            out, _ = self._run(account_id=ACCOUNT, show_account_id=True)
        self.assertIn(ACCOUNT, out)

    def test_failure_raises_command_error(self):
        with patch('runner.gm_adapter.GmBrokerAdapter', return_value=_FakeBroker({})):
            with self.assertRaises(CommandError):
                self._run(account_id=ACCOUNT)

    def test_no_published_accounts_is_informational(self):
        with patch('runner.gm_adapter.GmBrokerAdapter', return_value=_FakeBroker(FULL_CASH)):
            out, _ = self._run()
        self.assertIn('无需同步', out)

    def test_broker_init_failure_raises(self):
        with patch('runner.gm_adapter.GmBrokerAdapter', side_effect=RuntimeError('no token')):
            with self.assertRaisesRegex(CommandError, 'gm 账户查询通道'):
                self._run(account_id=ACCOUNT)


# ============ 预配置 gm user id：持仓快照与账户匹配（2026-10-09）============

class _PositionBroker(_FakeBroker):
    """带持仓的账户查询替身。"""

    def __init__(self, cash=None, positions=None, positions_error=None):
        super().__init__(cash=cash)
        self.positions = positions or []
        self.positions_error = positions_error

    def get_positions(self):
        if self.positions_error is not None:
            raise self.positions_error
        return self.positions


class NormalizePositionsTest(TestCase):
    """持仓归一：代码形态归一（gm 带交易所前缀）、空仓过滤、外部持仓判定。"""

    def test_gm_prefix_matches_plain_code(self):
        """gm 返回 SHSE.600000，系统存 600000 → 应判为纳管持仓（非外部）。"""
        snap = normalize_positions(
            [{'symbol': 'SHSE.600000', 'volume': 100, 'closep': 10.5, 'market_value': 1050}],
            ACCOUNT, known_symbols={'600000'},
        )
        self.assertEqual(snap.external_symbols, [])
        self.assertEqual(snap.positions[0]['is_external'], False)

    def test_external_position_detected(self):
        """不在系统纳管范围内的持仓 → 外部持仓（提示改用 cash 口径）。"""
        snap = normalize_positions(
            [{'symbol': 'HK.00700', 'volume': 50, 'value': 25000}],
            ACCOUNT, known_symbols={'600000', '000426'},
        )
        self.assertEqual(snap.external_symbols, ['HK.00700'])
        self.assertTrue(snap.as_dict()['has_external_position'])

    def test_zero_volume_row_ignored(self):
        """volume<=0 的空仓占位不算持仓。"""
        snap = normalize_positions(
            [{'symbol': 'SHSE.600519', 'volume': 0, 'closep': 1600}],
            ACCOUNT, known_symbols={'600519'},
        )
        self.assertEqual(snap.positions, [])
        self.assertEqual(snap.total_volume, Decimal('0'))

    def test_market_value_falls_back_to_price_times_volume(self):
        """缺 market_value 时用 价格×数量 推导（与 risk.py 字段回退一致）。"""
        snap = normalize_positions(
            [{'symbol': 'SHSE.600000', 'volume': 200, 'closep': 10}],
            ACCOUNT, known_symbols={'600000'},
        )
        self.assertEqual(snap.total_market_value, Decimal('2000'))

    def test_no_known_symbols_skips_external_judgement(self):
        """known_symbols 为空 → 不做外部判定（无法判断纳管范围）。"""
        snap = normalize_positions(
            [{'symbol': 'SHSE.600000', 'volume': 100}], ACCOUNT, known_symbols=None)
        self.assertEqual(snap.external_symbols, [])

    def test_empty_and_malformed_input(self):
        for raw in (None, [], 'x', [None, 'y', {}]):
            snap = normalize_positions(raw, ACCOUNT, known_symbols={'1'})
            self.assertEqual(snap.positions, [])


class SyncWithPositionsTest(TestCase):
    """持仓快照随资金同步一起写入账户配置行。"""

    databases = ['default', 'kline']

    def setUp(self):
        from apps.watchlists.models import Symbol
        Symbol.objects.get_or_create(code='600000', defaults={'name': '浦发', 'market': 'A', 'exchange': 'SSE'})

    def test_positions_written_to_config(self):
        broker = _PositionBroker(
            cash=FULL_CASH,
            positions=[{'symbol': 'SHSE.600000', 'volume': 100, 'closep': 10.5, 'market_value': 1050}],
        )
        sync_account_funds(ACCOUNT, broker)
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.position_count, 1)
        self.assertEqual(cfg.position_symbols, ['SHSE.600000'])
        self.assertFalse(cfg.has_external_position)
        self.assertIsNotNone(cfg.position_synced_at)

    def test_external_position_sets_basis_suggestion(self):
        """外部持仓 → 建议改 cash 口径（仅提示，不自动改用户选择）。"""
        broker = _PositionBroker(
            cash=FULL_CASH,
            positions=[{'symbol': 'HK.00700', 'volume': 50, 'value': 25000}],
        )
        result = sync_account_funds(ACCOUNT, broker)
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertTrue(cfg.has_external_position)
        self.assertEqual(cfg.basis_suggestion, 'cash')
        self.assertEqual(result.get('basis_suggestion'), 'cash')
        # 不自动改用户已选口径
        self.assertEqual(cfg.capital_basis, 'total')

    def test_position_failure_does_not_block_fund_sync(self):
        """持仓查询失败**不阻断**资金同步（资金是额度上限的主依据）。"""
        broker = _PositionBroker(cash=FULL_CASH, positions_error=RuntimeError('gm 持仓接口异常'))
        result = sync_account_funds(ACCOUNT, broker)
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(str(cfg.total_capital), '100000.00')
        self.assertIsNone(result['positions'])

    def test_position_failure_keeps_previous_snapshot(self):
        """持仓查询失败 → 保留上次快照（不误清空导致外部持仓提示消失）。"""
        first = _PositionBroker(
            cash=FULL_CASH,
            positions=[{'symbol': 'HK.00700', 'volume': 50, 'value': 25000}],
        )
        sync_account_funds(ACCOUNT, first)
        broker = _PositionBroker(cash=FULL_CASH, positions_error=RuntimeError('boom'))
        sync_account_funds(ACCOUNT, broker)
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.position_count, 1)
        self.assertTrue(cfg.has_external_position)

    def test_can_skip_positions(self):
        broker = _PositionBroker(cash=FULL_CASH,
                                 positions=[{'symbol': 'SHSE.600000', 'volume': 1}])
        sync_account_funds(ACCOUNT, broker, with_positions=False)
        cfg = AccountFundConfig.objects.get(account_id=ACCOUNT)
        self.assertEqual(cfg.position_count, 0)
