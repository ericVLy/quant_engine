from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0003_caseversion'),
        ('execution', '0007_order_external_order_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='NodeRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('node_type', models.CharField(choices=[('suite', 'Suite 节点'), ('case', 'Case 节点')], max_length=10)),
                ('status', models.CharField(choices=[('pending', '待执行'), ('running', '执行中'), ('completed', '已完成'), ('failed', '失败'), ('skipped', '已跳过')], default='pending', max_length=20)),
                ('direction', models.SmallIntegerField(default=0)),
                ('result', models.JSONField(blank=True, default=dict)),
                ('started_at', models.DateTimeField(auto_now_add=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('case', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='node_runs', to='cases.case')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='execution.noderun')),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='node_runs', to='execution.suiterun')),
                ('suite', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='node_runs', to='suites.suite')),
            ],
        ),
    ]
