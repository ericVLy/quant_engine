from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0002_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='CaseVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.PositiveIntegerField()),
                ('name', models.CharField(max_length=100)),
                ('node_type', models.CharField(choices=[('signal', '信号节点'), ('filter', '过滤器'), ('verdict', '裁决节点'), ('executor', '执行器')], max_length=20)),
                ('params', models.JSONField(default=dict)),
                ('status', models.CharField(choices=[('draft', '草稿'), ('published', '已发布'), ('archived', '已归档')], max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='cases.case')),
            ],
            options={
                'ordering': ('-version',),
                'constraints': [models.UniqueConstraint(fields=('case', 'version'), name='unique_case_version')],
            },
        ),
    ]