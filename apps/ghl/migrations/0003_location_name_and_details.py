from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ghl", "0002_portable_token_constraints")]

    operations = [
        migrations.RenameField(model_name="ghllocation", old_name="label", new_name="name"),
        migrations.AddField(model_name="ghllocation", name="company_id", field=models.TextField(blank=True, null=True)),
        migrations.AddField(model_name="ghllocation", name="email", field=models.TextField(blank=True, null=True)),
        migrations.AddField(model_name="ghllocation", name="phone", field=models.TextField(blank=True, null=True)),
        migrations.AddField(model_name="ghllocation", name="details", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="ghllocation", name="details_synced_at", field=models.DateTimeField(blank=True, null=True)),
    ]
