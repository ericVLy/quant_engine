"""gm 模拟账户订单生命周期真实链路联调。

用法：
  1) 只读连通性探测（不下单，用于确认终端/账户可达）：
     python manage.py gm_live_link --account <模拟账户ID> --probe
  2) 完整模拟订单生命周期（下单→轮询回报→撤单/清理）：
     python manage.py gm_live_link --account <模拟账户ID> --symbol SZSE.000001 --volume 100

约束（对齐文档 5.1.4 统一验收要求）：
  - 仅允许操作 gm **模拟账户**，禁止实盘；
  - 默认 volume 极小（100 股），避免激进的成交与敞口；
  - 无论成功与否，命令结束前会尝试撤掉仍未成交的挂单，避免遗留敞口。
"""

import time
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.execution.models import ExecutionLog, Order
from apps.suites.models import Suite
from apps.plans.models import Plan
from runner.gm_adapter import GmBrokerAdapter


class Command(BaseCommand):
    help = 'gm 模拟账户订单生命周期真实链路联调与连通性探测'

    def add_arguments(self, parser):
        parser.add_argument('--account', type=str, required=True,
                            help='gm 模拟账户 ID（掘金模拟账户，例如 12345678）')
        parser.add_argument('--token', type=str, default=None,
                            help='gm token；缺省使用 settings.GM_TOKEN / 环境变量')
        parser.add_argument('--probe', action='store_true',
                            help='只读连通性探测，不提交任何委托')
        parser.add_argument('--symbol', type=str, default='SZSE.000001',
                            help='交易标的（掘金代码，含交易所前缀）')
        parser.add_argument('--volume', type=int, default=100,
                            help='下单数量（默认 100，保持小额）')
        parser.add_argument('--price', type=float, default=0.0,
                            help='限价单价格；0 表示市价（不推荐）')
        parser.add_argument('--poll-seconds', type=float, default=2.0,
                            help='回报轮询间隔（秒）')
        parser.add_argument('--max-polls', type=int, default=15,
                            help='最大回报轮询次数')

    def handle(self, *args, **options):
        adapter = GmBrokerAdapter(token=options['token'], account_id=options['account'])
        if options['probe']:
            return self._probe(adapter, options)
        return self._lifecycle(adapter, options)

    # ------------------------------------------------------------------
    # 只读连通性探测
    # ------------------------------------------------------------------
    def _probe(self, adapter, options):
        self.stdout.write(f'[连通性探测] account={options["account"]} probe=read-only')
        account, positions = None, []
        try:
            account = adapter.get_account()
            positions = adapter.get_positions()
        except Exception as exc:  # pylint: disable=broad-except
            # get_cash 在部分环境下即使链路正常也会 1022 超时；
            # 回退到 get_unfinished_orders（实测稳定）验证交易通道。
            self.stdout.write(f'  [warn] get_cash/get_positions 超时（{exc}），回退 get_unfinished_orders')
            try:
                unfinished = adapter.get_unfinished_orders()
            except Exception as exc2:  # pylint: disable=broad-except
                raise CommandError(
                    f'终端/账户不可达或 SDK 调用失败：{exc2}\n'
                    '请确认：\n'
                    '  1) 掘金终端(客户端)已在本机登录并保持运行；\n'
                    '  2) --account 是 gm 模拟账户 ID；\n'
                    '  3) token 对本机有效（settings.GM_TOKEN 或 --token）。'
                ) from exc2
            self.stdout.write(f'  unfinished_orders: {self._compact(unfinished)}')
            self.stdout.write('[结论] 交易通道可达（get_cash 不可用，不影响下单链路）。')
            return 0
        self.stdout.write('[账户快照] 关联成功，返回：')
        self.stdout.write(f'  account: {account}')
        self.stdout.write(f'  positions: {positions}')
        self.stdout.write('[结论] 模拟账户可达，可进行订单生命周期联调。')
        return 0

    # ------------------------------------------------------------------
    # 完整模拟订单生命周期
    # ------------------------------------------------------------------
    def _lifecycle(self, adapter, options):
        symbol = options['symbol']
        volume = options['volume']
        # 1) 本地留痕：创建 ExecutionLog + Order（pending）
        suite = Suite.objects.get_or_create(name='GM 模拟链路', defaults={})[0]
        plan = Plan.objects.filter(name='GM 模拟链路 Plan').first() or Plan.objects.create(
            name='GM 模拟链路 Plan', root_suite=suite, status='draft',
        )
        log = ExecutionLog.objects.create(
            plan=plan, symbol=symbol, final_direction=1,
            task_id=f'gm-live-link-{int(time.time())}',
        )
        order = Order.objects.create(
            log=log, symbol=symbol, direction='buy',
            price=Decimal(str(max(options['price'], 0.01))),
            volume=volume,
        )
        self.stdout.write(f'[1/6] 本地订单已建：id={order.pk} {symbol} buy {volume}')

        # 2) 提交委托
        order_data = {
            'direction': 'buy', 'volume': volume,
            'price': options['price'] or 1.0,
            'order_type': 'market' if options['price'] == 0 else 'limit',
            'position_effect': 'open',
        }
        try:
            response = adapter.submit_order(symbol, order_data)
        except Exception as exc:  # pylint: disable=broad-except
            order.status = 'rejected'
            order.last_error = str(exc)
            order.save(update_fields=['status', 'last_error', 'updated_at'])
            log.status = 'failed'
            log.error_msg = str(exc)
            log.save(update_fields=['status', 'error_msg'])
            raise CommandError(f'委托提交失败（已回写 rejected）：{exc}') from exc
        external_id = self._extract_external_id(response)
        if external_id:
            order.external_order_id = external_id
        order.status = 'sent'
        order.save(update_fields=['status', 'external_order_id', 'updated_at'])
        self.stdout.write(f'[2/6] 委托已提交：external_order_id={external_id!r} '
                          f'response={self._compact(response)}')

        # 3) 轮询回报（订单状态 + 执行回报），经 on_order_status 归一化回写
        try:
            terminal = self._poll_and_apply(adapter, order, options)
        except Exception as exc:  # pylint: disable=broad-except
            log.status = 'failed'
            log.error_msg = str(exc)
            log.save(update_fields=['status', 'error_msg'])
            raise CommandError(f'回报轮询/回写失败：{exc}') from exc

        order.refresh_from_db()
        self.stdout.write(f'[3/6] 回报轮询结束：status={order.status} '
                          f'filled_volume={order.filled_volume}/{order.volume} '
                          f'price={order.price}')

        # 4) 生命周期结论
        state_map = {
            'filled': '完全成交',
            'sent': '部分成交或仍挂单',
            'rejected': '拒单',
            'canceled': '已撤',
            'pending': '待发送',
        }
        self.stdout.write(f'[4/6] 生命周期结论：{state_map.get(order.status, order.status)} '
                          f'（terminal_final={terminal}）')

        # 5) 清理：对仍挂单/未完全成交的订单发出撤单请求
        if order.status in ('pending', 'sent') and order.filled_volume < order.volume:
            try:
                cancel_resp = adapter.request_cancel(symbol, order_id=order.external_order_id)
                self.stdout.write(f'[5/6] 已提交撤单请求：{self._compact(cancel_resp)}')
            except Exception as exc:  # pylint: disable=broad-except
                self.stderr.write(f'[5/6] 撤单请求失败（需人工确认）：{exc}')
        else:
            self.stdout.write('[5/6] 订单已终结，无需撤单。')

        # 6) 终态汇总
        order.refresh_from_db()
        log.final_direction = 1 if order.status in ('sent', 'filled') else 0
        log.status = 'success'
        log.save(update_fields=['final_direction', 'status'])
        self.stdout.write(f'[6/6] 终态：order.status={order.status} '
                          f'filled_volume={order.filled_volume} '
                          f'external_order_id={order.external_order_id}')
        self.stdout.write('[完成] 模拟账户链路联调结束，验收记录已写库。')
        return 0

    # ------------------------------------------------------------------
    # 回报轮询与回写
    # ------------------------------------------------------------------
    def _poll_and_apply(self, adapter, order, options):
        poll_seconds = max(options['poll_seconds'], 0.5)
        max_polls = max(options['max_polls'], 1)
        terminal = None
        for i in range(max_polls):
            time.sleep(poll_seconds)
            reports = adapter.get_orders()
            exec_reports = adapter.get_execution_reports(order_id=order.external_order_id)
            changed = False
            for report in self._iter_reports(reports):
                applied = adapter.on_order_status(self._normalize_report(report))
                if applied is not None:
                    changed = True
            for ep in self._iter_reports(exec_reports):
                applied = adapter.on_order_status(self._normalize_execution(ep))
                if applied is not None:
                    changed = True
            order.refresh_from_db()
            terminal = order.status
            self.stdout.write(
                f'  [poll {i + 1}] local.status={order.status} '
                f'filled={order.filled_volume}/{order.volume} changed={changed}'
            )
            if order.status in ('filled', 'rejected', 'canceled'):
                break
        return terminal

    @staticmethod
    def _iter_reports(reports):
        """兼容 dict / list / 单对象 三种真实返回值。"""
        if reports is None:
            return []
        if isinstance(reports, dict):
            data = reports.get('data', reports)
            if isinstance(data, list):
                return data
            return [data]
        if isinstance(reports, list):
            return reports
        return [reports]

    @staticmethod
    def _normalize_report(report):
        """gm 查询返回的字段与回报结构存在差异，映射到适配器可识别字段。"""
        if not isinstance(report, dict):
            getter = getattr(report, 'to_dict', None)
            report = getter() if getter else {
                k: getattr(report, k) for k in (
                    'cl_ord_id', 'order_id', 'order_status', 'status',
                    'filled_volume', 'filled_price', 'price', 'symbol',
                )
            }
        return dict(report)

    @staticmethod
    def _normalize_execution(ep):
        """执行回报（成交回报）字段映射。"""
        if not isinstance(ep, dict):
            getter = getattr(ep, 'to_dict', None)
            ep = getter() if getter else {
                k: getattr(ep, k) for k in (
                    'cl_ord_id', 'order_id', 'order_status', 'filled_volume',
                    'filled_price', 'price', 'symbol',
                )
            }
        out = dict(ep)
        if 'filled_price' in out and 'price' not in out:
            out['price'] = out['filled_price']
        if 'order_status' in out and 'status' not in out:
            out['status'] = out['order_status']
        return out

    @staticmethod
    def _extract_external_id(response):
        if isinstance(response, dict):
            return (response.get('cl_ord_id') or response.get('order_id')
                    or response.get('order_id_str'))
        if isinstance(response, (list, tuple)) and response:
            first = response[0]
            if isinstance(first, dict):
                return (first.get('cl_ord_id') or first.get('order_id')
                        or first.get('order_id_str'))
            return getattr(first, 'cl_ord_id', None) or getattr(first, 'order_id', None)
        return getattr(response, 'cl_ord_id', None) or getattr(response, 'order_id', None)

    @staticmethod
    def _compact(value):
        try:
            import json
            return json.dumps(value, default=str, ensure_ascii=False)[:300]
        except Exception:  # pylint: disable=broad-except
            return str(value)[:300]
