"""部署初始化状态（SetupState）与「预建单行」。

两个要点
--------
1. **主键固定为 1**（不用auto increment）：本表是全局单行，``pk=1`` 让
   ``select_for_update().get(pk=1)`` 与 ``get_or_create`` 的语义稳定，不依赖自增值。
2. **迁移即预建单行**：若把首行留给运行时首次 ``get_or_create``，并发请求会同时尝试
   INSERT 同一 pk，在 SQLite 上抛 ``database table is locked``（表现为 500 而非正确的 403）。
   预建后并发只做 UPDATE，配合事务与行锁可正确地「一成功、其余 403」。
"""
from django.db import migrations, models


def _create_singleton(apps, schema_editor):
    """预建单行（idempotent：已存在则跳过）。"""
    SetupState = apps.get_model('users', 'SetupState')
    SetupState.objects.get_or_create(pk=1, defaults={'completed': False})


def _drop_singleton(apps, schema_editor):
    """反向：仅删除「未完成」的单行，已完成状态不回滚（避免误清初始化记录）。"""
    SetupState = apps.get_model('users', 'SetupState')
    SetupState.objects.filter(pk=1, completed=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SetupState',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, primary_key=True,
                                                        serialize=False, verbose_name='ID')),
                ('completed', models.BooleanField(default=False, verbose_name='已完成初始化')),
                ('completed_at', models.DateTimeField(blank=True, null=True, verbose_name='完成时间')),
                ('completed_by', models.CharField(blank=True, max_length=150, verbose_name='初始化管理员')),
                ('initialized_marker', models.CharField(blank=True, max_length=64, verbose_name='初始化标记')),
            ],
            options={
                'verbose_name': '部署初始化状态',
                'verbose_name_plural': '部署初始化状态',
                'db_table': 'users_setup_state',
            },
        ),
        migrations.RunPython(_create_singleton, _drop_singleton),
    ]
