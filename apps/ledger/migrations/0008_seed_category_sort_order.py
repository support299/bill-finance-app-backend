from django.db import migrations

# Same order as the seeded chart in 0002, so the P&L lists accounts in chart order instead of by amount.
ORDER = {
    "revenue": ["Sales Revenue", "Service Revenue", "Rental Revenue", "Sales Returns & Allowances"],
    "cogs": ["Cost of Goods Sold", "Freight & Shipping", "Direct Labor"],
    "operating_expense": [
        "Advertising & Marketing", "Auto & Vehicle", "Bank & Service Charges", "Contract Labor",
        "Depreciation & Amortization", "Dues & Subscriptions", "Education & Training", "Employee Benefits",
        "Insurance", "Meals & Entertainment", "Office Supplies & Expenses", "Payroll Taxes", "Professional Fees",
        "Rent or Lease", "Repairs & Maintenance", "Salaries & Wages", "Software & Technology",
        "Taxes & Licenses", "Travel", "Utilities", "Other Business Expenses",
    ],
    "other_income": ["Interest Income", "Dividend Income", "Gain on Sale of Assets", "Other Income"],
    "other_expense": ["Interest Expense", "Income Tax Expense", "Loss on Sale of Assets", "Other Expense", "Uncategorized"],
}


def seed(apps, schema_editor):
    Category = apps.get_model("ledger", "Category")
    for type_, names in ORDER.items():
        for i, name in enumerate(names, start=1):
            Category.objects.filter(user__isnull=True, type=type_, name=name).update(sort_order=i * 10)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0007_category_sort_order_attachment")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
