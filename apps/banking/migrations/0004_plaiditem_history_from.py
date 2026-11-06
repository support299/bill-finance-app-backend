from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("banking", "0003_reconciliation"),
    ]

    operations = [
        migrations.AddField(
            model_name="plaiditem",
            name="history_from",
            field=models.DateField(blank=True, null=True),
        ),
    ]
