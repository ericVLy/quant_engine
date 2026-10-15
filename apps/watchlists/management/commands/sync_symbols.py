"""把开发环境（SQLite）的标的同步到当前环境（通常是部署用的 MariaDB）。

背景
----
开发机用 SQLite（``db.sqlite3``），部署机用 MariaDB。两边的 ``Symbol`` 表靠人工
对齐，很容易出现"开发环境配好了一堆标的，部署环境是空的"——此时Web 界面没有任何
可选标的，功能无法使用。

设计取舍
--------
**在部署环境执行、只读开发库的 SQLite 文件**，而不是反过来：
Django 只配置了一个数据库连接，反向（开发机直连部署的 MariaDB）需要额外注入
部署库口令，风险高且易泄漏。故本命令在部署环境运行，用标准库 ``sqlite3``
以**只读**方式打开开发库文件，再通过 ORM 写入当前库。

服务必须先停
------------
``Symbol`` 虽是配置表，但 ``datasources.SymbolSnapshot`` 以它为主键且
``on_delete=CASCADE``，分时采样/调度器等后台进程也可能在同一时刻读写标的。
同步期间停服可避免"改到一半被后台进程读到中间状态"。

Windows 上通过 ``deploy/windows/services.ps1`` 停/启服务；该脚本会连带清理
残留进程。停服前会先记录哪些服务本来就在运行，**只恢复原来在跑的**——
不会把用户故意停掉的服务重新拉起来。

用法::

    # 基本用法（默认会先停服务、同步完再恢复）
    python manage.py sync_symbols --source "C:\\path\\to\\dev\\db.sqlite3"

    # 先看看会做什么，不写库不停服
    python manage.py sync_symbols --source ... --dry-run

    # 不同步期间停服，自己控制时机
    python manage.py sync_symbols --source ... --no-stop-services

    # 连带删除目标库中源库没有的标的（**会级联删掉快照数据，慎用**）
    python manage.py sync_symbols --source ... --prune

**失败语义**：源库不可读/表不存在 → 直接报错退出，不做任何写入；
写入过程出错 → 事务回滚，目标库保持原样。
"""
# pylint: disable=import-outside-toplevel  # 延迟导入以规避加载期副作用
import datetime
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.watchlists.models import Symbol

# 同步的字段：只取业务字段，不含 id / created_at / updated_at（由 ORM 处理）
SYNC_FIELDS = ('code', 'name', 'exchange', 'market')


class Command(BaseCommand):
    help = '把开发环境（SQLite）的标的同步到当前环境；同步前后自动停/启服务'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source', required=True,
            help='开发环境 SQLite 文件路径（如 ...\\quant_engine\\db.sqlite3）',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='只打印将要新增/更新/删除的内容，不写库、不停服务',
        )
        parser.add_argument(
            '--prune', action='store_true',
            help='删除目标库中源库不存在的标的（默认只增改不删）',
        )
        parser.add_argument(
            '--no-stop-services', action='store_true',
            help='同步期间不停服务（自行保证无并发读写）',
        )
        parser.add_argument(
            '--no-restart', action='store_true',
            help='同步后不恢复服务（与 --no-stop-services 搭配时无意义）',
        )
        parser.add_argument(
            '--keep-backup', action='store_true',
            help='写库前已在 .backup/ 留存快照，默认不再重复备份',
        )

    # ------------------------------------------------------------------ 主流程
    def handle(self, *args, **opts):
        src_path = self._resolve_source(opts['source'])
        table = Symbol._meta.db_table

        self.stdout.write(self.style.MIGRATE_HEADING(f'源库：{src_path}'))
        source_rows = self._read_source(src_path, table)
        if not source_rows:
            raise CommandError(
                f'源库里没有任何标的（表 {table} 为空）。请确认路径是否正确。'
            )
        self.stdout.write(f'  源库共{len(source_rows)} 个标的')

        target_map = self._read_target(table)
        plan = self._plan(source_rows, target_map, prune=opts['prune'])

        self._report(plan, opts['dry_run'])
        if not plan['created'] and not plan['updated'] and not plan['unchanged']:
            self.stdout.write(self.style.SUCCESS('目标库已是最新，无需写入。'))
            return
        if not (plan['created'] or plan['updated'] or plan['deleted']):
            self.stdout.write(self.style.SUCCESS('没有需要变更的标的。'))
            return

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('--dry-run：以上仅为预演，未做任何改动。'))
            return

        # ---------------------------------------------------------- 停服务
        was_running = {}
        if not opts['no_stop_services']:
            was_running = self._stop_services()
        elif not opts['no_restart']:
            self.stdout.write(self.style.WARNING(
                '已指定 --no-stop-services：不会自动恢复服务，请自行确认服务状态。'))
            opts['no_restart'] = True   # 未停就别启

        try:
            self._backup_target(plan, skip=opts['keep_backup'])
            with transaction.atomic():
                created, updated = self._write(source_rows, target_map)
                deleted = self._delete(plan['stale_codes']) if plan['deleted'] else 0
        except Exception as exc:                       # noqa: BLE001 需回滚并给出可读原因
            self.stderr.write(self.style.ERROR(f'写入失败，已回滚：{exc}'))
            raise CommandError(f'同步失败，目标库未变更：{exc}') from exc

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'同步完成：新增 {created}、更新 {updated}、未变 {len(plan["unchanged"])}'
            + (f'、删除 {deleted}' if plan['deleted'] else '')
        ))

        # ---------------------------------------------------------- 恢复服务
        if was_running:
            self._start_services(was_running)
        elif opts['no_stop_services'] and not opts['no_restart']:
            pass

    # ------------------------------------------------------------ 源/目标读取
    @staticmethod
    def _resolve_source(raw):
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise CommandError(f'源库文件不存在：{path}')
        if path.suffix.lower() != '.sqlite3':
            raise CommandError(
                f'源库应为 SQLite 文件（*.sqlite3），当前是：{path.name}。\n'
                '用法是「在部署环境执行、本命令去读开发库的 SQLite」，'
                '不需要也不支持直连开发机数据库。'
            )
        return path

    def _read_source(self, path, table):
        """以只读方式打开源 SQLite，读取标的行。

        用 ``mode=ro`` URI 打开，确保不会误写开发库。
        """
        uri = f'file:{path.as_posix()}?mode=ro'
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise CommandError(f'无法以只读方式打开源库：{exc}') from exc
        try:
            cur = conn.cursor()
            cur.execute(
                f'SELECT {", ".join(SYNC_FIELDS)} FROM {table} ORDER BY code'  # noqa: S608
            )
            rows = cur.fetchall()
        except sqlite3.Error as exc:
            raise CommandError(
                f'读取源库失败（表 {table} 不存在或字段不匹配）：{exc}\n'
                '请确认该文件确实是本项目的开发库 db.sqlite3。'
            ) from exc
        finally:
            conn.close()

        out = []
        for row in rows:
            item = dict(zip(SYNC_FIELDS, row))
            # 源库可能有 NULL（SQLite 允许），统一成空串，避免写入 NOT NULL 报错
            out.append({k: (v if v is not None else '') for k, v in item.items()})
        return out

    def _read_target(self, table):
        return {s.code: {'name': s.name, 'exchange': s.exchange, 'market': s.market}
                for s in Symbol.objects.all()}

    # ------------------------------------------------------------------ 计划
    @staticmethod
    def _plan(source_rows, target_map, prune):
        created, updated, unchanged, stale_codes = [], [], [], []
        seen = set()
        for row in source_rows:
            code = row['code']
            seen.add(code)
            cur = target_map.get(code)
            if cur is None:
                created.append(row)
            else:
                diff = {k: (cur.get(k), v) for k, v in row.items()
                        if k != 'code' and cur.get(k) != v}
                if diff:
                    updated.append((row, diff))
                else:
                    unchanged.append(code)
        if prune:
            stale_codes = [c for c in target_map if c not in seen]
        return {
            'created': created, 'updated': updated, 'unchanged': unchanged,
            'stale_codes': stale_codes,
            'deleted': len(stale_codes),
        }

    def _report(self, plan, dry_run):
        tag = '将' if dry_run else '待'
        self.stdout.write('')
        self.stdout.write(f'  新增 {len(plan["created"])} 个：')
        for row in plan['created']:
            self.stdout.write(f'    + {row["code"]:<10} {row["name"]}  [{row["market"]}]')
        if not plan['created']:
            self.stdout.write('    （无）')

        self.stdout.write(f'  更新 {len(plan["updated"])} 个：')
        for row, diff in plan['updated']:
            detail = '，'.join(f'{k}: {old!r} → {new!r}' for k, (old, new) in diff.items())
            self.stdout.write(f'    ~ {row["code"]:<10} {row["name"]}  ({detail})')
        if not plan['updated']:
            self.stdout.write('    （无）')

        self.stdout.write(f'  未变 {len(plan["unchanged"])} 个')
        if plan['deleted']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'  {tag}删除 {plan["deleted"]} 个（目标库有、源库没有）：'))
            for code in plan['stale_codes']:
                self.stdout.write(f'    - {code}')
            self.stdout.write(self.style.WARNING(
                '    ⚠ datasources.SymbolSnapshot 以 Symbol 为主键且 CASCADE，'
                '删除标的同时会删掉其快照数据。'))
        else:
            self.stdout.write('  删除 0 个（未指定 --prune）')

    # ------------------------------------------------------------------ 写入
    @staticmethod
    def _write(source_rows, target_map):
        created = updated = 0
        for row in source_rows:
            cur = target_map.get(row['code'])
            if cur is None:
                Symbol.objects.create(**row)
                created += 1
            elif any(cur.get(k) != v for k, v in row.items() if k != 'code'):
                Symbol.objects.filter(code=row['code']).update(
                    **{k: v for k, v in row.items() if k != 'code'})
                updated += 1
        return created, updated

    def _delete(self, codes):
        # 先报数量再删；CASCADE 影响已在 _report 里提示
        deleted, _ = Symbol.objects.filter(code__in=codes).delete()
        return deleted

    # ------------------------------------------------------------------ 备份
    def _backup_target(self, plan, skip=False):
        """写库前把目标库现有标的导出到 ``.backup/``，便于回退。"""
        if skip:
            return
        try:
            root = Path(__file__).resolve().parents[4]
            backup_dir = root / '.backup'
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            path = backup_dir / f'symbols_before_sync_{stamp}.json'
            payload = [{f: getattr(s, f) for f in SYNC_FIELDS}
                       for s in Symbol.objects.all()]
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding='utf-8')
            self.stdout.write(f'  已备份目标库现有 {len(payload)} 个标的 → {path.name}')
        except Exception as exc:                       # noqa: BLE001 备份失败不应阻断同步
            self.stderr.write(self.style.WARNING(f'备份失败（继续执行）：{exc}'))

    # -------------------------------------------------------------- 服务控制
    @staticmethod
    def _services_script():
        if sys.platform != 'win32':
            return None
        # <repo>/apps/watchlists/management/commands/sync_symbols.py -> repo root
        root = Path(__file__).resolve().parents[4]
        path = root / 'deploy' / 'windows' / 'services.ps1'
        return path if path.exists() else None

    def _stop_services(self):
        script = self._services_script()
        if script is None:
            self.stdout.write(self.style.WARNING(
                '未找到 deploy/windows/services.ps1（非 Windows 或未安装），'
                '请自行确保同步期间无并发读写。'))
            return {}

        running = self._probe_running()
        self.stdout.write('')
        self.stdout.write(f'  停服中（当前在运行：{", ".join(running) if running else "无"}）…')
        self._run_ps1(script, 'stop')
        return running

    def _start_services(self, running):
        script = self._services_script()
        if script is None:
            return
        if not running:
            self.stdout.write('  同步前没有服务在运行，保持停止状态。')
            return
        self.stdout.write(f'  恢复服务：{", ".join(running)}…')
        self._run_ps1(script, 'start', running)

    def _run_ps1(self, script, action, services=None):
        args = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(script), '-Action', action]
        if services:
            args += ['-Service', ','.join(services)]
        try:
            proc = subprocess.run(  # noqa: S603
                args, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=180, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.stderr.write(self.style.WARNING(f'调用 services.ps1 失败：{exc}'))
            return
        if proc.returncode != 0:
            self.stderr.write(self.style.WARNING(
                f'services.ps1 -{action} 返回 {proc.returncode}，请确认服务状态：'))
            for line in (proc.stdout or '').splitlines()[-8:]:
                self.stderr.write(f'    {line}')

    def _probe_running(self):
        """同步前记录哪些服务本来就在运行，避免把用户故意停的服务重新拉起。"""
        script = self._services_script()
        if script is None:
            return []
        try:
            proc = subprocess.run(  # noqa: S603
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', str(script), '-Action', 'status'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        running = []
        for line in (proc.stdout or '').splitlines():
            if '任务=Running' in line:
                running.append(line.split()[0].strip())
        return running
