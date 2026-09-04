from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('suites', '0003_suite_cases'),
    ]

    operations = [
        migrations.CreateModel(
            name='SuiteVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.PositiveIntegerField()),
                ('snapshot', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('suite', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='suites.suite')),
            ],
            options={
                'ordering': ('-version',),
                'constraints': [
                    models.UniqueConstraint(fields=('suite', 'version'), name='unique_suite_version'),
                ],
            },
        ),
    ]
