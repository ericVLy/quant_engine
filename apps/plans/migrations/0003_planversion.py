from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('plans', '0002_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlanVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.PositiveIntegerField()),
                ('snapshot', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='plans.plan')),
            ],
            options={
                'ordering': ('-version',),
                'constraints': [models.UniqueConstraint(fields=('plan', 'version'), name='unique_plan_version')],
            },
        ),
    ]