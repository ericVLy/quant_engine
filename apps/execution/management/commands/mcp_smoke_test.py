"""MCP 端到端冒烟测试（`manage.py mcp_smoke_test`）。

用真实 MCP 协议（SSE 客户端）连到运行中的 MCP 服务，跑通全链路：

    建夹具（Case×2 → Suite → 拓扑 → Plan） → 发布 → 读侧回读校验
      →（可选）受控触发 → 删除保护校验 → 清理

覆盖 MCP 写（``create_case`` / ``create_suite`` / ``update_suite_topology`` /
``create_plan`` / ``delete_*``）、读（``list_*`` / ``get_*``）与受控触发
（``trigger_plan_execution``）工具，编排逻辑见 ``mcp_server.smoke``。

前置条件：
    1. 目标 MCP 服务已启动且**已开启配置写门禁**（``--allow-mutate``）；
       需要 ``--trigger`` 时还要 ``--allow-trigger``。
       推荐：``manage.py run_dev_stack --allow-mutate --allow-trigger``；
    2. ``--symbol`` 指定的标的已存在于 ``watchlists.Symbol``。

示例：
    .venv/bin/python manage.py mcp_smoke_test
    .venv/bin/python manage.py mcp_smoke_test --account-id <gm账户ID> \\
        --allocated-capital 50000 --trigger
    .venv/bin/python manage.py mcp_smoke_test --keep    # 保留夹具供人工核对

退出码：全部步骤通过为 0；任一步失败抛 ``CommandError``（打印已完成步骤与已创建主键，
并按依赖逆序尽力清理，避免残留半成品）。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
import asyncio
import json
import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = '对运行中的 MCP 服务做端到端冒烟测试（建夹具 → 发布 → 校验 → 触发 → 清理）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--url', default=None,
            help='MCP SSE 端点，默认按 MCP_HOST/MCP_PORT 组合为 '
                 'http://<host>:<port>/sse（缺省 127.0.0.1:8765）',
        )
        parser.add_argument(
            '--auth-token', dest='auth_token', default=None,
            help='Bearer 令牌，默认取 MCP_AUTH_TOKEN；服务端开启鉴权时必填',
        )
        parser.add_argument(
            '--account-id', dest='account_id', default='',
            help='绑定到 Plan 的交易账户 ID（留空=不绑定账户、不占用资金；'
                 '提供时若该账户无 AccountFundConfig 会自动建一行默认总资金配置）',
        )
        parser.add_argument(
            '--allocated-capital', dest='allocated_capital', type=float, default=None,
            help='Plan 占用资金（大于 0 时触发账户空闲资金校验）；留空=不占用',
        )
        parser.add_argument(
            '--symbol', default='000426',
            help='Plan 标的范围使用的标的代码（须已入库），默认 000426',
        )
        parser.add_argument(
            '--no-publish', dest='publish', action='store_false', default=True,
            help='只建 draft 夹具不发布（默认发布；未发布时读侧校验跳过已发布断言）',
        )
        parser.add_argument(
            '--trigger', action='store_true', default=False,
            help='额外执行受控触发（需 MCP 服务开启 --allow-trigger；'
                 '仅创建 pending SuiteRun，不会下单）',
        )
        parser.add_argument(
            '--keep', action='store_true', default=False,
            help='保留夹具不清理（默认建完即清，便于反复运行）',
        )

    # ------------------------------------------------------------------ 入口
    def handle(self, *args, **options):
        from mcp_server.smoke import McpSmokeRunner, SseMcpSession

        url = options['url'] or self._default_url()
        headers = {}
        token = options['auth_token'] or os.getenv('MCP_AUTH_TOKEN', '')
        if token:
            headers['Authorization'] = f'Bearer {token}'

        runner = McpSmokeRunner(
            account_id=options['account_id'],
            allocated_capital=options['allocated_capital'],
            symbol_code=options['symbol'],
            publish=options['publish'],
            trigger=options['trigger'],
            keep=options['keep'],
            emit=self.stdout.write,
        )

        self.stdout.write(f'[smoke] 目标 MCP 服务: {url}')
        try:
            runner.prepare_account()
            asyncio.run(self._phase_create(runner, SseMcpSession, url, headers))
            if options['publish']:
                runner.publish_fixture()
            asyncio.run(self._phase_verify(runner, SseMcpSession, url, headers))
            if not options['keep']:
                runner.purge_runs()
                asyncio.run(self._phase_delete(runner, SseMcpSession, url, headers))
        except Exception as exc:  # pylint: disable=broad-except
            self._report_failure(runner, exc)
            if not options['keep']:
                self._best_effort_cleanup(runner, SseMcpSession, url, headers)
            raise CommandError(f'MCP 冒烟测试失败: {exc}') from exc

        self.stdout.write(self.style.SUCCESS('[smoke] 全部步骤通过'))
        self.stdout.write(json.dumps({
            'steps': runner.steps,
            'created': runner.ids,
            'created_run_ids': runner.created_run_ids,
            'kept': options['keep'],
        }, ensure_ascii=False))

    # -------------------------------------------------------------- 阶段划分
    @staticmethod
    async def _phase_create(runner, session_cls, url, headers):
        async with session_cls(url, headers) as session:
            await runner.check_tools(session)
            await runner.create_fixture(session)

    @staticmethod
    async def _phase_verify(runner, session_cls, url, headers):
        async with session_cls(url, headers) as session:
            await runner.verify_reads(session)
            if runner.trigger:
                await runner.trigger_execution(session)
            await runner.check_delete_protection(session)

    @staticmethod
    async def _phase_delete(runner, session_cls, url, headers):
        async with session_cls(url, headers) as session:
            await runner.delete_fixture(session)

    # ---------------------------------------------------------------- 辅助
    @staticmethod
    def _default_url() -> str:
        host = os.getenv('MCP_HOST', '127.0.0.1')
        port = os.getenv('MCP_PORT', '8765')
        return f'http://{host}:{port}/sse'

    def _report_failure(self, runner, exc) -> None:
        self.stderr.write(self.style.ERROR(f'[smoke] 失败于步骤 {runner.steps}: {exc}'))
        if runner.ids:
            self.stderr.write('[smoke] 已创建主键: ' + json.dumps(runner.ids, ensure_ascii=False))

    def _best_effort_cleanup(self, runner, session_cls, url, headers) -> None:
        """失败后按依赖逆序尽力清理已创建对象（清理失败只提示，不掩盖原始错误）。"""
        try:
            runner.purge_runs()
        except Exception as exc:  # pylint: disable=broad-except
            self.stderr.write(f'[smoke] 清理 SuiteRun 失败: {exc}')
        try:
            asyncio.run(self._phase_delete(runner, session_cls, url, headers))
        except Exception as exc:  # pylint: disable=broad-except
            self.stderr.write(f'[smoke] 失败后清理夹具未完成: {exc}')
