from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('execution', '0008_noderun'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', '待发送'),
                    ('sent', '已发送'),
                    ('filled', '已成交'),
                    ('rejected', '已拒绝'),
                    ('canceled', '已撤单'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]