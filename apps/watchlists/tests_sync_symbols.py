"""``sync_symbols`` 管理命令的测试。

命令的核心契约：
- 以**只读**方式打开源 SQLite，绝不写开发库；
- 源库不可用/为空 → 明确报错，**不做任何写入**；
- 默认只增改不删；``--prune`` 才删除，且要报告级联影响；
- ``--dry-run`` 既不写库也不碰服务。
"""
import sqlite3
import tempfile
from pathlib import Path
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.watchlists.management.commands import sync_symbols
from apps.watchlists.models import Symbol

TABLE = Symbol._meta.db_table


def _make_source_db(path, rows):
    """构造一个"开发库"SQLite 文件。``rows`` 为 (code, name, exchange, market) 元组列表。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            f'CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, code varchar(20), '
            'name varchar(100), exchange varchar(20), market varchar(10))'
        )
        conn.executemany(
            f'INSERT INTO {TABLE} (code, name, exchange, market) VALUES (?,?,?,?)',
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


class SyncSymbolsTests(TestCase):
    """用目标库（测试库）当"部署环境"，临时 SQLite 当"开发环境"。"""

    def setUp(self):
        # pylint: disable=consider-using-with
        # setUp 里无法用 with（每个用例独立持有目录），addCleanup 是等价的
        # 且更稳妥——即使 setUp 后续步骤抛错，清理也已注册。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src_path = Path(self.tmp.name) / 'dev.sqlite3'

    # ------------------------------------------------------------------ 工具
    def _run(self, *extra, source=None):
        out = StringIO()
        err = StringIO()
        call_command(
            'sync_symbols',
            '--source', str(source or self.src_path),
            '--no-stop-services',           # 测试里不碰真实服务
            *extra,
            stdout=out, stderr=err,
        )
        return out.getvalue(), err.getvalue()

    # ------------------------------------------------------------------ 基本
    def test_creates_missing_symbols(self):
        _make_source_db(self.src_path, [
            ('000426', '兴业银锡', 'SZ', 'A'),
            ('688017', '绿的谐波', 'SH', 'A'),
        ])
        self._run()
        self.assertEqual(Symbol.objects.count(), 2)
        s = Symbol.objects.get(code='000426')
        self.assertEqual(s.name, '兴业银锡')
        self.assertEqual(s.exchange, 'SZ')
        self.assertEqual(s.market, 'A')

    def test_updates_changed_fields_only(self):
        _make_source_db(self.src_path, [('000426', '新名称', 'SZ', 'A')])
        Symbol.objects.create(code='000426', name='旧名称', exchange='SZ', market='A')
        self._run()
        s = Symbol.objects.get(code='000426')
        self.assertEqual(s.name, '新名称')
        self.assertEqual(Symbol.objects.count(), 1, '不应产生重复行')

    def test_untouched_symbol_stays_unchanged(self):
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        Symbol.objects.create(code='000426', name='兴业银锡', exchange='SZ', market='A')
        before = Symbol.objects.get(code='000426').updated_at
        self._run()
        after = Symbol.objects.get(code='000426')
        self.assertEqual(after.name, '兴业银锡')
        # 幂等：再次运行不应产生任何变更
        self._run()
        self.assertEqual(Symbol.objects.get(code='000426').updated_at, after.updated_at)
        self.assertIsNotNone(before)

    def test_second_run_is_idempotent(self):
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        self._run()
        self._run()
        self.assertEqual(Symbol.objects.count(), 1)

    # -------------------------------------------------------------- dry-run
    def test_dry_run_does_not_write(self):
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        out, _ = self._run('--dry-run')
        self.assertEqual(Symbol.objects.count(), 0, 'dry-run 不应写库')
        self.assertIn('000426', out)
        self.assertIn('新增 1 个', out)

    def test_dry_run_reports_updates_with_old_and_new(self):
        _make_source_db(self.src_path, [('000426', '新名称', 'SZ', 'A')])
        Symbol.objects.create(code='000426', name='旧名称', exchange='SZ', market='A')
        out, _ = self._run('--dry-run')
        self.assertIn('旧名称', out)
        self.assertIn('新名称', out)

    # ----------------------------------------------------------------- prune
    def test_prune_deletes_stale_symbols(self):
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        Symbol.objects.create(code='999999', name='旧标的', exchange='SZ', market='A')
        self._run('--prune')
        codes = set(Symbol.objects.values_list('code', flat=True))
        self.assertEqual(codes, {'000426'})

    def test_without_prune_keeps_stale_symbols(self):
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        Symbol.objects.create(code='999999', name='旧标的', exchange='SZ', market='A')
        self._run()
        self.assertEqual(Symbol.objects.count(), 2, '默认不应删除')

    def test_prune_output_warns_about_cascade(self):
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        Symbol.objects.create(code='999999', name='旧标的', exchange='SZ', market='A')
        out, _ = self._run('--dry-run', '--prune')
        self.assertIn('999999', out)
        self.assertIn('CASCADE', out.upper())

    # ------------------------------------------------------------ 错误路径
    def test_missing_source_file_errors(self):
        with self.assertRaisesMessage(CommandError, '不存在'):
            self._run(source=Path(self.tmp.name) / 'nope.sqlite3')
        self.assertEqual(Symbol.objects.count(), 0)

    def test_non_sqlite_extension_rejected(self):
        bad = Path(self.tmp.name) / 'dev.db'
        bad.write_text('not a db', encoding='utf-8')
        with self.assertRaisesMessage(CommandError, 'sqlite3'):
            self._run(source=bad)

    def test_empty_source_errors_without_writing(self):
        _make_source_db(self.src_path, [])
        with self.assertRaisesMessage(CommandError, '没有任何标的'):
            self._run()
        self.assertEqual(Symbol.objects.count(), 0)

    def test_source_without_symbol_table_errors(self):
        conn = sqlite3.connect(str(self.src_path))
        conn.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        with self.assertRaisesMessage(CommandError, '读取源库失败'):
            self._run()
        self.assertEqual(Symbol.objects.count(), 0)

    def test_source_is_opened_read_only(self):
        """命令不得写入开发库。"""
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        before = self.src_path.read_bytes()
        self._run()
        self.assertEqual(self.src_path.read_bytes(), before,
                         '源库文件不应被修改')

    # ------------------------------------------------- 服务控制路径（易漏分支）
    def test_stop_start_services_path_runs_without_error(self):
        """覆盖停服/启服代码路径。

        这条曾经漏测，导致 ``_run_ps1`` 里在 ``@staticmethod`` 中使用 ``self``
        的 NameError 一直潜伏到真实执行才暴露。

        注意必须**首次就有变更**才能走到服务控制逻辑——无变更时命令会提前
        返回且不停服（这是有意设计：不该为no-op 停服务）。
        """
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])

        calls = []

        class _FakeProc:
            returncode = 0
            stdout = 'Web        任务=Running   进程数=2 端口 8000 已监听\n'
            stderr = ''

        def _fake_run(args, **kwargs):
            calls.append(args)
            return _FakeProc()

        with mock.patch.object(sync_symbols.subprocess, 'run', _fake_run), \
             mock.patch.object(sync_symbols.Command, '_services_script',
                               staticmethod(lambda: Path(__file__).resolve().parents[2])):
            call_command('sync_symbols', '--source', str(self.src_path),
                         '--keep-backup',
                         stdout=StringIO(), stderr=StringIO())

        self.assertEqual(Symbol.objects.count(), 1)
        actions = [a[a.index('-Action') + 1] for a in calls if '-Action' in a]
        self.assertIn('status', actions, '同步前应先探测哪些服务在运行')
        self.assertIn('stop', actions, '同步前应停服')
        self.assertEqual(actions[-1], 'start', '同步后应恢复服务')

    def test_no_change_does_not_stop_services(self):
        """无变更时应提前返回、不停服务（不该为 no-op 打扰线上）。"""
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])
        self._run('--no-stop-services')          # 首次写入
        Symbol.objects.get(code='000426')        # 确认已存在

        calls = []
        with mock.patch.object(sync_symbols.subprocess, 'run',
                               lambda args, **kw: calls.append(args)), \
             mock.patch.object(sync_symbols.Command, '_services_script',
                               staticmethod(lambda: Path(__file__).resolve().parents[2])):
            call_command('sync_symbols', '--source', str(self.src_path),
                         '--keep-backup',
                         stdout=StringIO(), stderr=StringIO())

        actions = [a[a.index('-Action') + 1] for a in calls if '-Action' in a]
        self.assertEqual(actions, [], '无变更时不应调用 services.ps1')

    def test_stopped_services_are_not_restarted(self):
        """探测到"确实都没在跑"时，不应把它们拉起来。"""
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])

        class _FakeProc:
            returncode = 0
            # 三个服务都明确是 Ready —— 探测**有效**，且答案为"都没跑"
            stdout = ('Web        任务=Ready     进程数=0 端口 8000 未监听\n'
                      'Scheduler  任务=Ready     进程数=0 \n'
                      'Nginx      任务=Ready     进程数=0 端口 8080 未监听\n')
            stderr = ''

        calls = []
        with mock.patch.object(sync_symbols.subprocess, 'run',
                               lambda args, **kw: (calls.append(args), _FakeProc())[1]), \
             mock.patch.object(sync_symbols.Command, '_services_script',
                               staticmethod(lambda: Path(__file__).resolve().parents[2])):
            call_command('sync_symbols', '--source', str(self.src_path),
                         '--keep-backup',
                         stdout=StringIO(), stderr=StringIO())

        actions = [a[a.index('-Action') + 1] for a in calls if '-Action' in a]
        self.assertIn('stop', actions)
        self.assertNotIn('start', actions, '探测确认都没在跑时，不应启动')

    def test_running_detection_survives_mojibake_output(self):
        """status 输出是系统 ANSI（GBK），按 utf-8 解码后中文是乱码。

        因此探测**只能按 ASCII 片段**匹配。曾经按中文 ``任务=Running`` 匹配，
        永远匹配不到 → 运行中的服务被误判为空 → 停服后不恢复，线上停摆。
        """
        cmd = sync_symbols.Command()
        # 模拟 GBK 字节被当 utf-8 解码后的乱码：中文全变问号，ASCII 完好
        mojibake = ('  Web        ???=Running   ??=2 ???? 8000 ???\n'
                    '  Scheduler  ???=Running   ??=2 \n'
                    '  Nginx      ???=Ready     ??=0 ???? 8080 ???\n')
        with mock.patch.object(sync_symbols.subprocess, 'run',
                               lambda *a, **k: mock.Mock(stdout=mojibake, returncode=0)):
            self.assertEqual(cmd._probe_running(), ['Web', 'Scheduler'])

    def test_ambiguous_probe_fails_safe_by_starting_all(self):
        """探测失效（解析不出任何服务）时应兜底拉起全部。

        "让线上停摆"的后果远重于"多启动一个服务"，故取保服务的一侧。
        """
        _make_source_db(self.src_path, [('000426', '兴业银锡', 'SZ', 'A')])

        class _FakeProc:
            returncode = 0
            stdout = ''          # 探测彻底失效：没有任何可解析内容
            stderr = ''

        calls = []
        with mock.patch.object(sync_symbols.subprocess, 'run',
                               lambda args, **kw: (calls.append(args), _FakeProc())[1]), \
             mock.patch.object(sync_symbols.Command, '_services_script',
                               staticmethod(lambda: Path(__file__).resolve().parents[2])):
            out = StringIO()
            call_command('sync_symbols', '--source', str(self.src_path),
                         '--keep-backup', stdout=out, stderr=StringIO())

        actions = [a[a.index('-Action') + 1] for a in calls if '-Action' in a]
        self.assertIn('stop', actions)
        self.assertEqual(actions[-1], 'start', '探测失效时应兜底启动')
        self.assertIn('兜底', out.getvalue())
