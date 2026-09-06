from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('execution', '0009_alter_order_status_choices'),
    ]

    operations = [
        migrations.AddField(
            model_name='executionlog',
            name='error_code',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='executionlog',
            name='task_id',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='order',
            name='filled_volume',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='order',
            name='last_error',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='order',
            name='report_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]