"""修复迁移历史被压缩前遗留的老库：补齐 execution 两张表缺失的列并纠正被污染的默认值。

背景（2026-10-09）
------------------
本项目曾把各 app 的迁移历史**压缩为每 app 一个 initial** 并**重建数据库**。压缩后的
``0001_initial`` 已经包含 ``EventTypeRegistry.base_event_type`` 与
``AccountFundConfig`` 的 source / capital_basis / available_cash / market_value /
frozen_cash / synced_at。因此**在这批压缩迁移之前创建的旧库**里，模型有字段而实际表缺列，
而``makemigrations --check`` 报「无差异」（Django 认为 0001 已应用）——一个静默缺口。

实际症状：``GET /api/execution/event-types/list-all/`` 持续 500
``OperationalError: no such column: execution_event_type_registry.base_event_type``
（``EventRegistry._get_cache`` 每次都查该表，故前端「事件类型管理」页整页报错）。

为什么不用标准 ``AddField``
---------------------------
标准 ``AddField`` 在 SQLite 上走 ``_remake_table``：它把「新列 → 旧列」与「新列 → 默认值」
混在同一�� ``mapping`` 里，再 ``INSERT INTO new SELECT ... FROM old``。当默认值取成
``quote_name(列名)`` 时，SQLite 对**不存在的双引号标识符不会报错、而是回退成字符串字面量**，
于是旧行的新列会被填成``'capital_basis'`` 这种**列名本身**（实测复现）。
这会静默污染 ``NOT NULL`` 的 ``capital_basis``/``source``——比缺列更危险。

因此本迁移改为「自己发``ALTER TABLE ADD COLUMN`` + 显式修数据」：
``AddColumnIfAbsent`` 只负责补列（列已存在则跳过，故新建库上无副作用）；
``RunSQL`` 把任何被写成列名字面量的历史脏值改回正确默认值/NULL。

这样既能在老库上修好，又不会在按压缩迁移新建的库上重复加列或污染数据。
"""
from django.db import migrations, models

# 被SQLite 回退成「列名字面量」的脏值。正常值只可能是下列集合之外的内容。
_DIRTY = {
    "base_event_type": {"base_event_type"},
    "capital_basis": {"capital_basis"},
    "source": {"source"},
    "available_cash": {"available_cash"},
    "market_value": {"market_value"},
    "frozen_cash": {"frozen_cash"},
    "synced_at": {"synced_at"},
}


def _existing_columns(schema_editor, table_name):
    with schema_editor.connection.cursor() as cursor:
        return {
            col.name
            for col in schema_editor.connection.introspection.get_table_description(
                cursor, table_name
            )
        }


class AddColumnIfAbsent(migrations.AddField):
    """AddField 的幂等变体：目标列已存在则跳过（压缩迁移新建的库即属此类）。"""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if model._meta.get_field(self.name).column in _existing_columns(
            schema_editor, model._meta.db_table
        ):
            return
        # 复用 Django 自身的建表/加列实现，保证类型与约束与模型一致。
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.model_name)
        if model._meta.get_field(self.name).column not in _existing_columns(
            schema_editor, model._meta.db_table
        ):
            return
        super().database_backwards(app_label, schema_editor, from_state, to_state)


def repair_dirty_defaults(apps, schema_editor):
    """把被 SQLite 回退成「列名字面量」的脏值改回正确默认值 / NULL。

    逐表检查列是否存在，逐列只修正「值等于列名自身」的行；幂等可重复执行。
    """
    db = schema_editor.connection
    tables = db.introspection.table_names()
    # 有默认值的列 → 回落默认值；可空列 → 置 NULL
    fallback = {"capital_basis": "total", "source": "manual"}
    for table in ("execution_event_type_registry", "execution_accountfundconfig"):
        if table not in tables:
            continue
        columns = _existing_columns(schema_editor, table)
        for column in columns & set(_DIRTY):
            with db.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = %s", [column]
                )
                if not cursor.fetchone()[0]:
                    continue
                if column in fallback:
                    cursor.execute(
                        f"UPDATE {table} SET {column} = %s WHERE {column} = %s",
                        [fallback[column], column],
                    )
                else:
                    cursor.execute(
                        f"UPDATE {table} SET {column} = NULL WHERE {column} = %s",
                        [column],
                    )


def noop_reverse(apps, schema_editor):
    """反向不做删列：删列会丢数据，且这些列本就属于模型定义。"""


class Migration(migrations.Migration):

    dependencies = [
        ("execution", "0002_initial"),
    ]

    operations = [
        AddColumnIfAbsent(
            model_name="eventtyperegistry",
            name="base_event_type",
            field=models.CharField(
                blank=True,
                help_text="用户自定义事件必须叠加在系统自带事件之上（base_event_type 为系统内置事件名）；插件事件可选叠加。",
                max_length=50,
                null=True,
                verbose_name="叠加基事件",
            ),
        ),
        AddColumnIfAbsent(
            model_name="accountfundconfig",
            name="source",
            field=models.CharField(
                choices=[("manual", "手工维护"), ("gm", "gm 同步")],
                default="manual",
                max_length=10,
                verbose_name="资金来源",
            ),
        ),
        AddColumnIfAbsent(
            model_name="accountfundconfig",
            name="capital_basis",
            field=models.CharField(
                choices=[
                    ("total", "账户总资产（账面资金 + 持仓市值）"),
                    ("cash", "账面资金（忽略持仓市值）"),
                    ("available", "券商可用资金（最保守）"),
                ],
                default="total",
                max_length=10,
                verbose_name="额度口径",
            ),
        ),
        AddColumnIfAbsent(
            model_name="accountfundconfig",
            name="available_cash",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=18,
                null=True,
                verbose_name="券商可用资金",
            ),
        ),
        AddColumnIfAbsent(
            model_name="accountfundconfig",
            name="market_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=18,
                null=True,
                verbose_name="持仓市值",
            ),
        ),
        AddColumnIfAbsent(
            model_name="accountfundconfig",
            name="frozen_cash",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=18,
                null=True,
                verbose_name="冻结资金",
            ),
        ),
        AddColumnIfAbsent(
            model_name="accountfundconfig",
            name="synced_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="最近同步时间"
            ),
        ),
        migrations.RunPython(repair_dirty_defaults, noop_reverse),
    ]
