from django.db import migrations


def backfill(apps, schema_editor):
    """GHL invoice tax lines were posted without a category; file the uncategorized ones under Sales Tax Payable."""
    Category = apps.get_model("ledger", "Category")
    Transaction = apps.get_model("ledger", "Transaction")
    cat = Category.objects.filter(user__isnull=True, name="Sales Tax Payable", type="liability").first()
    if cat:
        Transaction.objects.filter(
            source="ghl", external_ref__startswith="ghl:inv:", external_ref__endswith=":tax", user_category__isnull=True
        ).update(user_category=cat)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0008_seed_category_sort_order")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
