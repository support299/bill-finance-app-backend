import pytest

from apps.accounts.models import User
from apps.banking.models import BankAccount, PlaidItem
from apps.banking.services.adopt import adopt_imported
from apps.banking.services.sync import sync_item
from apps.ghl.models import GhlInvoice, GhlInvoicePayment, GhlLocation
from apps.ghl.services.lovable_import import ImportError_, import_location
from apps.ledger.models import Category, Ledger, Transaction

pytestmark = pytest.mark.django_db
LOC = "locABC"
U = "00000000-0000-0000-0000-0000000000"


def uid(n):
    return f"{U}{n:02d}"


def tx(n, date, amount, desc, **kw):
    base = {
        "id": uid(n),
        "plaid_item_id": uid(1),
        "plaid_account_id": "old-acct",
        "plaid_transaction_id": f"old-ptx-{n}",
        "merchant_name": desc,
        "original_description": desc,
        "description": desc,
        "amount": amount,
        "currency": "USD",
        "transaction_date": date,
        "authorized_date": None,
        "pending": False,
        "user_category_id": uid(70),
        "ledger_id": uid(50),
        "source": "plaid",
        "status": "posted",
        "gaap_type": "revenue" if amount > 0 else "operating_expense",
        "is_income": amount > 0,
        "is_expense": amount < 0,
        "is_transfer": False,
        "is_recurring": False,
        "is_hidden": False,
        "is_demo": False,
        "removed_at": None,
        "external_ref": None,
        "accounting_status": None,
        "due_date": None,
    }
    return {**base, **kw}


def export():
    return {
        "format": 1,
        "location": {"location_id": LOC, "label": "Imax Digital", "active": True},
        "categories": [{"id": uid(70), "name": "Sales Revenue", "type": "revenue", "is_system": True}],
        "ledgers": [{"id": uid(50), "label": "Acme Co", "account_class": "current_asset", "ghl_contact_id": "c1"}],
        "plaid_items": [
            {"id": uid(1), "item_id": "old-item", "institution_id": "ins_1", "institution_name": "Bank", "environment": "production"}
        ],
        "bank_accounts": [
            {
                "id": uid(2),
                "plaid_item_id": uid(1),
                "plaid_account_id": "old-acct",
                "institution_id": "ins_1",
                "account_name": "Business",
                "mask": "4204",
                "account_type": "depository",
                "current_balance": 833.01,
            }
        ],
        "transactions": [
            tx(10, "2026-03-01", 100, "STRIPE PAYOUT"),
            tx(11, "2026-03-02", -5, "COFFEE"),
            tx(12, "2026-03-02", -5, "COFFEE", plaid_transaction_id="old-ptx-12b"),  # genuine same-day twin
            tx(
                13,
                "2026-03-03",
                50,
                "Invoice INV-1",
                source="ghl",
                plaid_item_id=None,
                plaid_account_id=None,
                plaid_transaction_id=None,
                external_ref="ghl:inv:1",
            ),
            tx(14, "2026-03-04", -9, "REMOVED", removed_at="2026-03-05T00:00:00Z"),
        ],
        "ghl_invoices": [{"id": uid(60), "location_id": LOC, "ghl_invoice_id": "inv1", "ledger_id": uid(50), "total": 50, "raw": {}}],
        "ghl_invoice_payments": [{"id": uid(61), "invoice_id": uid(60), "ghl_transaction_id": "gt1", "amount": 50, "raw": {}}],
        "ghl_invoice_events": [{"id": uid(62), "invoice_id": uid(60), "ghl_invoice_id": "inv1", "source": "webhook", "payload": {}}],
    }


def test_import_is_idempotent_and_keeps_data(settings):
    first = import_location(export(), LOC)
    assert first["transactions_created"] == 4 and first["transactions_skipped_removed"] == 1
    assert first["ledgers_created"] == 1 and first["invoices_created"] == 1 and first["bank_connections_created"] == 1
    user = User.objects.get(email=f"ghl-{LOC.lower()}@tenants.invalid")
    assert Transaction.objects.filter(user=user).count() == 4  # same-day twin kept
    t = Transaction.objects.get(pk=uid(10))
    assert t.user_category.name == "Sales Revenue" and t.ledger.label == "Acme Co" and t.plaid_item.access_token_encrypted == ""
    assert PlaidItem.objects.get(item_id="old-item").connection_status == "imported"

    again = import_location(export(), LOC)
    assert again.get("transactions_created", 0) == 0 and again["transactions_already_imported"] == 4
    assert again.get("ledgers_created", 0) == 0 and again.get("invoices_created", 0) == 0
    assert Transaction.objects.filter(user=user).count() == 4
    assert (GhlInvoice.objects.count(), GhlInvoicePayment.objects.count(), Ledger.objects.count()) == (1, 1, 1)
    assert Category.objects.filter(name="Sales Revenue").count() == 1  # shared category reused


def test_dry_run_saves_nothing():
    stats = import_location(export(), LOC, dry_run=True)
    assert stats["transactions_created"] == 4
    assert (
        not Transaction.objects.exists()
        and not GhlLocation.objects.exists()
        and not User.objects.filter(email__endswith="tenants.invalid").exists()
    )


def test_skips_what_a_live_sync_or_invoice_sync_already_created():
    import_location(export(), LOC, dry_run=True)
    loc = GhlLocation.objects.create(location_id=LOC)
    user = User.objects.create_user(f"ghl-{LOC.lower()}@tenants.invalid", None)
    loc.user = user
    loc.save()
    live_item = PlaidItem.objects.create(user=user, item_id="new-item", institution_id="ins_1", access_token_encrypted="enc")
    live_acct = BankAccount.objects.create(
        user=user, plaid_item=live_item, plaid_account_id="new-acct", mask="4204", account_type="depository"
    )
    Transaction.objects.create(
        user=user,
        plaid_item=live_item,
        plaid_account_id="new-acct",
        plaid_transaction_id="new-ptx",
        amount=100,
        transaction_date="2026-03-01",
        original_description="STRIPE PAYOUT",
        source="plaid",
    )
    Transaction.objects.create(user=user, amount=50, transaction_date="2026-03-03", source="ghl", external_ref="ghl:inv:1")
    stats = import_location(export(), LOC)
    assert stats["transactions_duplicate_of_live_sync"] == 1 and stats["transactions_duplicate_ref"] == 1
    assert stats["transactions_created"] == 2  # the two coffees
    assert PlaidItem.objects.filter(user=user).count() == 1  # attached to the live connection, no placeholder
    assert Transaction.objects.filter(user=user, original_description="COFFEE").first().plaid_account_id == live_acct.plaid_account_id


def test_location_owned_by_another_bank_owner_is_refused():
    other = User.objects.create_user("o@example.com", "pw-12345678")
    PlaidItem.objects.create(user=other, item_id="old-item", access_token_encrypted="x")
    with pytest.raises(ImportError_):
        import_location(export(), LOC)
    with pytest.raises(ImportError_):
        import_location(export(), "someOtherLocation")


def test_reconnect_merges_history_without_duplicates():
    import_location(export(), LOC)
    user = User.objects.get(email=f"ghl-{LOC.lower()}@tenants.invalid")
    t = Transaction.objects.get(pk=uid(10))
    t.user_category_id = Category.objects.get(name="Sales Revenue").id
    t.save()
    # the bank is connected again: Plaid sends fresh rows (new ids), one of them brand new
    new_item = PlaidItem.objects.create(user=user, item_id="new-item", institution_id="ins_1", access_token_encrypted="enc")
    BankAccount.objects.create(user=user, plaid_item=new_item, plaid_account_id="new-acct", mask="4204", account_type="depository")
    for n, (date, amt, desc) in enumerate(
        [("2026-03-01", 100, "STRIPE PAYOUT"), ("2026-03-02", -5, "COFFEE"), ("2026-03-02", -5, "COFFEE"), ("2026-03-09", -20, "NEW THING")]
    ):
        Transaction.objects.create(
            user=user,
            plaid_item=new_item,
            plaid_account_id="new-acct",
            plaid_transaction_id=f"new-ptx-{n}",
            amount=amt,
            transaction_date=date,
            original_description=desc,
            source="plaid",
        )
    stats = adopt_imported(new_item)
    assert stats["merged"] == 3
    plaid_rows = Transaction.objects.filter(user=user, source="plaid")
    assert plaid_rows.count() == 4  # 3 merged + 1 genuinely new, no doubles
    payout = Transaction.objects.get(pk=uid(10))
    assert payout.plaid_transaction_id == "new-ptx-0" and payout.plaid_item_id == new_item.id and payout.ledger.label == "Acme Co"
    assert not PlaidItem.objects.filter(item_id="old-item").exists()  # placeholder fully taken over
    assert BankAccount.objects.filter(user=user).count() == 1


def test_placeholder_never_breaks_syncing():
    import_location(export(), LOC)
    item = PlaidItem.objects.get(item_id="old-item")
    assert sync_item(item.id).status == "skipped"
