from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0005_transaction_source_csv"),
    ]

    operations = [
        migrations.AddField(model_name="project", name="start_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="project", name="end_date", field=models.DateField(blank=True, null=True)),
    ]
