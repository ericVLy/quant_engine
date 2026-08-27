from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('execution', '0006_alter_event_event_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='external_order_id',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]
