"""把开发环境的 K 线数据同步到当前环境。

背景
----
开发机 K 线在 ``kline.sqlite3``，部署机在独立的 ``quant_kline`` 库（MariaDB）。
手工重建这些历史数据非常耗时，因此需要一条从开发环境搬运的命令。

K 线是**按标的动态分表**的（``kline_a_000426``、``kline_a_sh000001`` …），
表在首次写入时由 ``ensure_kline_table`` 用原生 SQL 创建，**不属于任何
registered model**。本命令因此：

- 复用 :func:`~apps.datasources.models.get_kline_table_name` 与
  :func:`~apps.datasources.models.get_runtime_kline_model`，
  不自己拼建表 SQL，避免与运行期逻辑分叉；
- 表名里已含市场与代码，直接据此定位标的，**无需再连开发库主库**。

⚠️ 跨环境必须按 **code** 映射，绝不能按 ``symbol_id``
----------------------------------------------------------------
分表的 ``symbol_id`` 是指向 ``Symbol`` 主键的外键，而两边的自增 ID
**必然不同**（实测开发库 id = 1,3,4,5…11，中间有删除；部署库是新插入的 1..10）。
若照搬 id，所有 K 线都会挂到错误的标的上——而且不会报错。

用法::

    # 预演
    python manage.py sync_klines --source "C:\\path\\to\\dev\\kline.sqlite3" --dry-run

    # 正式同步（自动停服 → 写入 → 恢复服务）
    python manage.py sync_klines --source "C:\\path\\to\\dev\\kline.sqlite3"

    # 只同步指定标的
    ... --codes 000426,sh000001

**前置条件**：目标库必须已存在对应标的，请先执行 ``sync_symbols``。
缺失的标的会被跳过并明确报出（不会静默丢数据）。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避加载期副作用
import datetime
import sqlite3
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.datasources.models import (
    ensure_kline_table,
    get_kline_database_alias,
    get_kline_table_name,
    get_runtime_kline_model,
)
from apps.watchlists.models import Symbol
from quant_engine.deployment import ServiceControlMixin

# 不参与搬运的列：主键、外键，以及由 ORM 维护的时间戳
SKIP_FIELDS = {'id', 'symbol_id', 'created_at', 'updated_at'}
TABLE_PREFIX = 'kline_'
# 市场代码 → 表名片段（与 get_kline_table_name 的 market_key 一致）
MARKET_KEYS = ('a', 'hk', 'us')


def _as_date(value):
    """把源库读出的日期统一成 ``datetime.date``。

    sqlite3 有时给回``str``、有时给 ``date``/``datetime``；目标侧 Django 也会
    归一成 ``date``。两侧必须归一到同一类型，否则对比全部失配、
    每次同步都会把"未变"当成"新增"，造成重复写入。
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def _as_value(field, value):
    """按字段类型归一化源库的值，使其与模型字段可比。

    ⚠️ Decimal 字段必须**按字段精度量化**，否则幂等性会坏掉：

    SQLite 把 ``amount`` 存成 float，Python 读出来是 ``1275406657.4199998``
    （二进制浮点噪声）；而目标列是 ``DECIMAL(20,2)``，落库后是
    ``1275406657.42``。直接比较两者永远不相等——于是每次同步都会把
    这些行判成"已变更"并重复写入，既慢又会把 updated_at 全部刷新。

    实测该现象会让 9/10 个分表每次都报几十到上百条"更新"。
    """
    if value is None:
        return None
    name = getattr(field, 'name', field)
    internal = getattr(field, 'get_internal_type', lambda: '')()
    if internal == 'DecimalField':
        try:
            dec = Decimal(str(value))
        except InvalidOperation as exc:
            raise CommandError(f'字段 {name} 的值 {value!r} 不是合法小数') from exc
        places = getattr(field, 'decimal_places', None)
        if places is not None:
            dec = dec.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        return dec
    if internal == 'DateField':
        return _as_date(value)
    return value


class Command(ServiceControlMixin, BaseCommand):
    help = '把开发环境（SQLite）的 K 线分表数据同步到当前环境'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source', required=True,
            help='开发环境 kline.sqlite3 路径（注意是 K 线库，不是主库 db.sqlite3）',
        )
        parser.add_argument(
            '--codes', default='',
            help='只同步指定标的（逗号分隔代码，如 000426,sh000001）；默认全部',
        )
        parser.add_argument('--dry-run', action='store_true',
                            help='只打印计划，不写库不停服')
        parser.add_argument('--prune', action='store_true',
                            help='删除目标表中源库没有的交易日（**会删真实行情**）')
        parser.add_argument('--no-stop-services', action='store_true',
                            help='同步期间不停服务')
        parser.add_argument('--no-restart', action='store_true',
                            help='同步后不恢复服务')

    # ------------------------------------------------------------------ 主流程
    def handle(self, *args, **opts):
        src = self._resolve_source(opts['source'])
        wanted = {c.strip() for c in opts['codes'].split(',') if c.strip()}

        self.stdout.write(self.style.MIGRATE_HEADING(f'源库：{src}'))
        plan = self._build_plan(src, wanted, prune=opts['prune'])
        self._report(plan)

        if not plan['work']:
            self.stdout.write(self.style.SUCCESS('目标库已是最新，无需写入。'))
            return
        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('--dry-run：以上仅为预演，未做任何改动。'))
            return

        was_running = None
        if not opts['no_stop_services']:
            was_running = self.stop_services()
        if opts['no_stop_services']:
            self.stdout.write(self.style.WARNING(
                '已指定 --no-stop-services：不会自动恢复服务，请自行确认服务状态。'))
            opts['no_restart'] = True

        created = updated = unchanged = deleted = 0
        try:
            for item in plan['work']:
                added, upd, same, gone = self._apply_one(item, prune=opts['prune'])
                created += added
                updated += upd
                unchanged += same
                deleted += gone
                self.stdout.write(
                    f'  {item["code"]:<10} 新增 {added:>5} / 更新 {upd:>5} / 未变 {same:>5}'
                    + (f' / 删除 {gone:>5}' if gone else '')
                )
        except Exception as exc:                       # noqa: BLE001 需回滚并给出可读原因
            self.stderr.write(self.style.ERROR(f'写入失败：{exc}'))
            if not (opts['no_stop_services'] or opts['no_restart']):
                self.restore_services(was_running)
            raise CommandError(f'同步失败：{exc}') from exc

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'K 线同步完成：新增 {created} 根、更新 {updated} 根、未变 {unchanged} 根'
            + (f'、删除 {deleted} 根' if deleted else '')
        ))

        if not (opts['no_stop_services'] or opts['no_restart']):
            self.restore_services(was_running)

    # -------------------------------------------------------------- 源库读取
    @staticmethod
    def _resolve_source(raw):
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise CommandError(f'源库文件不存在：{path}')
        if 'kline' not in path.name.lower():
            raise CommandError(
                f'看起来不像 K 线库（文件名应含 kline，如 kline.sqlite3）：{path.name}\n'
                '注意 K 线数据在独立的 kline.sqlite3 中，主库 db.sqlite3 里没有分表。'
            )
        return path

    @staticmethod
    def _connect_ro(path):
        """以只读方式打开源库，确保不会误写开发环境。"""
        uri = f'file:{path.as_posix()}?mode=ro'
        try:
            return sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise CommandError(f'无法以只读方式打开源库：{exc}') from exc

    @staticmethod
    def _code_from_table(table_name):
        """从分表名反解标的代码：``kline_a_sh000001`` -> ``sh000001``。"""
        if not table_name.startswith(TABLE_PREFIX):
            return ''
        rest = table_name[len(TABLE_PREFIX):]
        for key in MARKET_KEYS:
            if rest.startswith(f'{key}_'):
                return rest[len(key) + 1:]
        return ''

    # ------------------------------------------------------------------ 计划
    # pylint: disable=too-many-locals
    def _build_plan(self, src_path, wanted, prune):
        conn = self._connect_ro(src_path)
        plan = {'work': [], 'noop': [], 'missing': [], 'empty': [], 'source_rows': 0}
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (f'{TABLE_PREFIX}%',)).fetchall()]

            for table_name in sorted(names):
                code = self._code_from_table(table_name)
                if not code or (wanted and code not in wanted):
                    continue

                symbol = Symbol.objects.filter(code=code).first()
                if symbol is None:
                    plan['missing'].append((table_name, code))
                    continue

                n_rows = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
                if not n_rows:
                    plan['empty'].append((table_name, code))
                    continue

                model = get_runtime_kline_model(symbol)
                fields = [f for f in model._meta.fields if f.name not in SKIP_FIELDS]
                columns = [f.name for f in fields]
                col_sql = ', '.join(f'"{c}"' for c in columns)

                source_rows = []
                for raw in conn.execute(
                        f'SELECT {col_sql} FROM "{table_name}" ORDER BY "date"').fetchall():
                    item = {}
                    for field, raw_val in zip(fields, raw):
                        item[field.name] = _as_value(field, raw_val)
                    source_rows.append(item)

                existing = self._target_rows(model)
                to_insert, to_update, unchanged = [], [], 0
                for row in source_rows:
                    cur = existing.get(row['date'])
                    if cur is None:
                        to_insert.append(row)
                    elif self._differs(cur, row, columns):
                        to_update.append(row)
                    else:
                        unchanged += 1
                stale = sorted(set(existing) - {r['date'] for r in source_rows})

                plan['work'].append({
                    'code': code, 'symbol': symbol, 'model': model,
                    'source_table': table_name,
                    'target_table': get_kline_table_name(symbol),
                    'insert': to_insert, 'update': to_update,
                    'unchanged': unchanged, 'stale': stale if prune else [],
                    'source_count': n_rows,
                    'actionable': bool(to_insert or to_update or (prune and stale)),
                })
                plan['source_rows'] += n_rows
                if not (to_insert or to_update or (prune and stale)):
                    # 全部未变 → 不算"待处理"，否则会为no-op 白停一次服务
                    plan['noop'].append(plan['work'][-1])
                    plan['work'].pop()
        except sqlite3.Error as exc:
            raise CommandError(f'读取源库失败：{exc}') from exc
        finally:
            conn.close()
        return plan

    @staticmethod
    def _target_rows(model):
        """目标表已有数据：``{date: {列: 值}}``；表尚未创建时返回空字典。

        必须走 ``kline`` 数据库别名——K 线分表建在独立库（quant_kline），
        用默认库会报 "Table 'quant_engine.kline_xxx' doesn't exist"。

        ``list(...)`` 不能省：Django 的 QuerySet 是**惰性求值**的，真正的
        SQL 在迭代时才发出；若不强制求值，异常会发生在 try 之外。
        """
        columns = [f.name for f in model._meta.fields if f.name not in SKIP_FIELDS]
        alias = get_kline_database_alias()
        try:
            rows = list(model.objects.using(alias).values(*columns))
        except Exception:  # noqa: BLE001 目标分表还不存在（首次同步）
            return {}
        out = {}
        for row in rows:
            date = row.get('date')
            out[_as_date(date) if date is not None else None] = row
        return out

    @staticmethod
    def _differs(current, new_row, columns):
        for col in columns:
            old, new = current.get(col), new_row.get(col)
            if old is None and new is None:
                continue
            try:
                if Decimal(str(old)) != Decimal(str(new)):
                    return True
            except (InvalidOperation, TypeError, ValueError):
                if old != new:
                    return True
        return False

    # ------------------------------------------------------------------ 写入
    def _apply_one(self, item, prune):
        symbol, model = item['symbol'], item['model']
        alias = get_kline_database_alias()
        # 分表是运行期用原生 SQL 建的，首次同步时目标库还没有
        ensure_kline_table(symbol)
        manager = model.objects.using(alias)
        created = updated = deleted = 0
        with transaction.atomic(using=alias):
            for row in item['insert']:
                manager.create(symbol_id=symbol.pk, **row)
                created += 1
            for row in item['update']:
                manager.filter(symbol_id=symbol.pk, date=row['date']).update(
                    **{k: v for k, v in row.items() if k != 'date'})
                updated += 1
            if prune and item['stale']:
                deleted, _ = manager.filter(
                    symbol_id=symbol.pk, date__in=item['stale']).delete()
        return created, updated, item['unchanged'], deleted

    # ------------------------------------------------------------------ 报告
    def _report(self, plan):
        self.stdout.write('')
        if plan['missing']:
            self.stdout.write(self.style.WARNING(
                f'  跳过 {len(plan["missing"])} 个分表：目标库没有对应标的'
                '（请先执行 sync_symbols）'))
            for table_name, code in plan['missing']:
                self.stdout.write(f'    - {table_name}（代码 {code}）')
        if plan['empty']:
            self.stdout.write(f'  跳过 {len(plan["empty"])} 个空分表：'
                              + ', '.join(c for _, c in plan['empty']))
        self.stdout.write(
            f'  待处理分表 {len(plan["work"])} 个，源库共 {plan["source_rows"]} 根 K 线')
        for item in plan['work'] + plan['noop']:
            mark = '' if item.get('actionable', True) else '（无变化）'
            self.stdout.write(
                f'    {item["code"]:<10} 源 {item["source_count"]:>5} 根 → '
                f'新增 {len(item["insert"]):>5} / 更新 {len(item["update"]):>5} / '
                f'未变 {item["unchanged"]:>5}'
                + (f' / 待删 {len(item["stale"]):>5}' if item['stale'] else '')
                + mark
            )
