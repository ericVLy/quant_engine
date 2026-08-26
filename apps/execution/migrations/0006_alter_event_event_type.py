from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('execution', '0005_alter_suiterun_plan_alter_suiterun_suite'),
    ]

    operations = [
        migrations.AlterField(
            model_name='event',
            name='event_type',
            field=models.CharField(db_index=True, max_length=50),
        ),
    ]
