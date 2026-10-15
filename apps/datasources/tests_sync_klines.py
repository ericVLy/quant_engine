"""``sync_klines`` 管理命令的测试。

命令的核心契约：
- 标的按 **code** 匹配，绝不沿用源库的 ``symbol_id``（两边自增 ID 必然不同）；
- 一切读写走 ``kline`` 数据库别名（分表建在独立库，不是主库）；
- 源库以只读方式打开；
- 目标分表不存在时自动创建（分表是运行期原生 SQL 建的）；
- 目标库缺对应标的时**明确报出并跳过**，不静默丢数据。
"""
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.datasources.management.commands import sync_klines
from apps.datasources.models import (
    ensure_kline_table,
    get_kline_database_alias,
    get_runtime_kline_model,
)
from apps.watchlists.models import Symbol

# A 股分表字段（与运行期模型一致）
A_COLUMNS = ('date', 'open', 'high', 'low', 'close', 'volume', 'amount',
             'adj_factor', 'limit_up', 'limit_down', 'turnover_rate')


def _make_kline_db(path, tables):
    """构造"开发库"K线文件。``tables`` = ``{表名: [行元组]}``，元组顺序同 A_COLUMNS。

    每次调用都**重建**文件——同一个用例里常要多次改写源库来模拟增量同步。
    """
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        for name, rows in tables.items():
            cols = ', '.join(f'"{c}"' for c in A_COLUMNS)
            conn.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, {cols})')
            if rows:
                placeholders = ', '.join('?' * len(A_COLUMNS))
                conn.executemany(
                    f'INSERT INTO "{name}" ({cols}) VALUES ({placeholders})', rows)
        conn.commit()
    finally:
        conn.close()
    return str(path)


def _row(date, close=10.0, volume=1000):
    return (date, 10.0, 11.0, 9.0, close, volume, 10000.0, 1.0, None, None, None)


class SyncKlinesTests(TestCase):
    # K 线分表位于独立库，默认 TestCase 只允许访问 default，会直接抛
    # DatabaseOperationForbidden。get_runtime_kline_model 自身也做内省查询。
    databases = '__all__'

    def setUp(self):
        # ``models._RUNTIME_KLINE_MODELS`` 是**模块级缓存**，会跨测试存活；
        # 而测试库每个用例都重建，缓存里的模型会指向已不存在的分表，
        # 表现为 "no such table: kline_xxx"。故每个用例先清空它。
        from apps.datasources import models as ds_models
        ds_models._RUNTIME_KLINE_MODELS.clear()  # pylint: disable=protected-access
        self.addCleanup(ds_models._RUNTIME_KLINE_MODELS.clear)  # pylint: disable=protected-access

        self.tmp = tempfile.TemporaryDirectory()
        # pylint: disable=consider-using-with
        # setUp 里无法用 with（每个用例独立持有目录），addCleanup 是等价且更稳妥的
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / 'kline.sqlite3'
        self.alias = get_kline_database_alias()
        self.symbol = Symbol.objects.create(code='000426', name='兴业银锡',
                                           exchange='SZ', market='A')

    def _run(self, *extra, source=None):
        out, err = StringIO(), StringIO()
        call_command('sync_klines', '--source', str(source or self.src),
                     '--no-stop-services', *extra, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _target(self, code='000426'):
        sym = Symbol.objects.get(code=code)
        ensure_kline_table(sym)
        return get_runtime_kline_model(sym).objects.using(self.alias)

    # ------------------------------------------------------------- 表名解析
    def test_code_parsed_from_table_name(self):
        self.assertEqual(sync_klines.Command._code_from_table('kline_a_000426'), '000426')
        self.assertEqual(sync_klines.Command._code_from_table('kline_a_sh000001'), 'sh000001')
        self.assertEqual(sync_klines.Command._code_from_table('kline_hk_00700'), '00700')
        self.assertEqual(sync_klines.Command._code_from_table('kline_us_aapl'), 'aapl')
        self.assertEqual(sync_klines.Command._code_from_table('django_migrations'), '')

    # ---------------------------------------------------------------- 基本
    def test_creates_rows_on_first_sync(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05'), _row('2026-01-06')]})
        out, _ = self._run()
        self.assertEqual(self._target().count(), 2)
        self.assertIn('K 线同步完成', out)
        self.assertIn('新增 2', out)

    def test_second_run_is_idempotent(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})
        self._run()
        self._run()
        out, _ = self._run()
        self.assertIn('目标库已是最新', out)
        self.assertEqual(self._target().count(), 1)

    def test_changed_row_is_updated_not_duplicated(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05', close=10.0)]})
        self._run()
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05', close=12.34)]})
        self._run()
        rows = self._target().filter(date='2026-01-05')
        self.assertEqual(rows.count(), 1, '同一日期不应产生重复行')
        self.assertEqual(float(rows.first().close), 12.34)

    def test_maps_by_code_not_symbol_id(self):
        """目标库 Symbol 主键与源库不同也必须挂到正确标的下。"""
        Symbol.objects.create(code='OTHER', name='别的标的', exchange='SH', market='A')
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})
        self._run()
        # 源库分表里的 symbol_id 是开发库的 id（此处必然不等于目标库的自增 id）
        row = self._target().first()
        self.assertEqual(row.symbol_id, self.symbol.pk)
        other = get_runtime_kline_model(Symbol.objects.get(code='OTHER'))
        self.assertEqual(other.objects.using(self.alias).count(), 0)

    def test_writes_to_kline_database_alias(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})
        self._run()
        # 主库里不应出现该分表
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type='table' AND name='kline_a_000426'")
            self.assertEqual(cur.fetchone()[0], 0,
                             'K 线分表不应建在主库，必须走 kline 别名')

    # ------------------------------------------------------------ 跳过/过滤
    def test_float_precision_does_not_break_idempotency(self):
        """源库 SQLite 把 Decimal 字段存成 float，必须量化到字段精度。

        回归场景：``amount`` 读出来是 ``1275406657.4199998``（二进制浮点噪声），
        目标列是 DECIMAL(20,2) 落库成 ``1275406657.42``。不量化的话每次同步
        都会把这些行判成"已变更"重复写入。
        """
        noisy = 1275406657.4199998        # float 噪声
        _make_kline_db(self.src, {'kline_a_000426': [
            ('2026-01-05', 10.0, 11.0, 9.0, 10.5, 1000, noisy, 1.0, None, None, 1.23456789),
        ]})
        self._run()
        first = self._target().first()
        self.assertEqual(str(first.amount), '1275406657.42')
        # turnover_rate 是 DECIMAL(8,4)，同样要被量化
        self.assertEqual(str(first.turnover_rate), '1.2346')

        out, _ = self._run()
        self.assertIn('目标库已是最新', out,
                      '量化后第二次同步必须是 no-op，否则每次都会重复写入')
        out2, _ = self._run()
        self.assertIn('目标库已是最新', out2)
    def test_skips_table_without_matching_symbol(self):
        _make_kline_db(self.src, {
            'kline_a_000426': [_row('2026-01-05')],
            'kline_a_999999': [_row('2026-01-05')],
        })
        out, _ = self._run()
        self.assertIn('999999', out)
        self.assertIn('没有对应标的', out)
        self.assertEqual(self._target().count(), 1)

    def test_skips_empty_table(self):
        _make_kline_db(self.src, {'kline_a_000426': [], 'kline_a_000603': [_row('2026-01-05')]})
        out, _ = self._run()
        self.assertIn('空分表', out)

    def test_codes_filter(self):
        Symbol.objects.create(code='000603', name='盛达资源', exchange='SZ', market='A')
        _make_kline_db(self.src, {
            'kline_a_000426': [_row('2026-01-05')],
            'kline_a_000603': [_row('2026-01-05')],
        })
        self._run('--codes', '000426')
        self.assertEqual(self._target().count(), 1)
        other = get_runtime_kline_model(Symbol.objects.get(code='000603'))
        self.assertEqual(other.objects.using(self.alias).count(), 0)

    # ---------------------------------------------------------------- prune
    def test_prune_deletes_rows_absent_from_source(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})
        self._run()
        _make_kline_db(self.src, {'kline_a_000426': []})
        self._run()
        self.assertEqual(self._target().count(), 1, '空源表会被整体跳过，不应误删')
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-06')]})
        self._run('--prune')
        self.assertEqual([r.date.isoformat() for r in self._target()], ['2026-01-06'])

    def test_without_prune_keeps_extra_rows(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})
        self._run()
        _make_kline_db(self.src, {'kline_a_000426': []})
        Symbol.objects.get(code='000426')
        # 直接手工塞一根源库没有的行
        m = get_runtime_kline_model(self.symbol)
        m.objects.using(self.alias).create(symbol_id=self.symbol.pk, date='2026-02-02',
                                          open=1, high=1, low=1, close=1, volume=1)
        self._run('--codes', '000426')
        self.assertEqual(self._target().count(), 2, '默认不应删除')

    # ------------------------------------------------------------ 错误路径
    def test_missing_file_errors(self):
        with self.assertRaisesMessage(CommandError, '不存在'):
            self._run(source=Path(self.tmp.name) / 'nope.sqlite3')

    def test_non_kline_filename_rejected(self):
        bad = Path(self.tmp.name) / 'db.sqlite3'
        bad.write_text('x', encoding='utf-8')
        with self.assertRaisesMessage(CommandError, 'kline'):
            self._run(source=bad)

    def test_source_is_opened_read_only(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})
        before = self.src.read_bytes()
        self._run()
        self.assertEqual(self.src.read_bytes(), before, '源库文件不应被修改')

    def test_dry_run_does_not_write(self):
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})
        out, _ = self._run('--dry-run')
        self.assertIn('未做任何改动', out)
        # 目标分表甚至不该被创建
        from django.db import connection
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM sqlite_master "
                            "WHERE type='table' AND name='kline_a_000426'")
                self.assertEqual(cur.fetchone()[0], 0)
        except Exception:  # noqa: BLE001 表本就不存在也算通过
            pass

    # ---------------------------------------------------------- 服务控制路径
    def test_service_control_path(self):
        from quant_engine import deployment
        _make_kline_db(self.src, {'kline_a_000426': [_row('2026-01-05')]})

        class _FakeProc:
            returncode = 0
            stdout = ('Web        ???=Running   ??=2 ???? 8000 ???\n'
                      'Scheduler  ???=Running   ??=2 \n')
            stderr = ''

        calls = []
        with mock.patch.object(deployment.subprocess, 'run',
                               lambda args, **kw: (calls.append(args), _FakeProc())[1]), \
             mock.patch.object(deployment.ServiceControlMixin, 'services_script',
                               staticmethod(lambda: Path(__file__).resolve().parents[2])):
            call_command('sync_klines', '--source', str(self.src),
                         stdout=StringIO(), stderr=StringIO())

        actions = [a[a.index('-Action') + 1] for a in calls if '-Action' in a]
        self.assertEqual(actions, ['status', 'stop', 'start'])
        # 恢复时必须用逗号串（-File 不解析数组）
        start_args = next(a for a in calls if 'start' in a)
        self.assertIn('Web,Scheduler', start_args)
