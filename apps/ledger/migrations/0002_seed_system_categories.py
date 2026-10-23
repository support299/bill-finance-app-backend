from django.db import migrations

# Final GAAP chart of accounts from the original app (supabase migration 20261002...).
CHART = {
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
    "asset": ["Equipment & Fixed Assets", "Investment Purchase", "Loans to Others"],
    "liability": ["Credit Card Payment", "Loan Proceeds", "Loan Principal Repayment", "Sales Tax Payable"],
    "equity": ["Owner Contribution", "Owner Draw"],
    "transfer": ["Internal Transfer"],
}


def seed(apps, schema_editor):
    Category = apps.get_model("ledger", "Category")
    for type_, names in CHART.items():
        for name in names:
            Category.objects.get_or_create(user=None, name=name, type=type_, defaults={"is_system": True})


def unseed(apps, schema_editor):
    apps.get_model("ledger", "Category").objects.filter(user__isnull=True, is_system=True).delete()


class Migration(migrations.Migration):
    dependencies = [("ledger", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
