from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('cases', '0002_initial'),
        ('suites', '0002_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='suite',
            name='cases',
            field=models.ManyToManyField(
                blank=True,
                related_name='suites',
                to='cases.case',
            ),
        ),
    ]
