"""Double entry (expand stage): bank-synced rows become balanced journal entries; nothing else changes."""

import datetime
from decimal import Decimal

import pytest
from django.db.models import Sum

from apps.banking.models import BankAccount, PlaidItem
from apps.ledger.models import Account, Category, JournalEntry, JournalLine, Transaction
from apps.ledger.services.gaap import gaap_flags
from apps.ledger.services.journal import backfill

pytestmark = pytest.mark.django_db
D = datetime.date


@pytest.fixture
def banks(user_a):
    item = PlaidItem.objects.create(user=user_a, item_id="it1", access_token_encrypted="x")
    mk = lambda pid, name, typ: BankAccount.objects.create(  # noqa: E731
        user=user_a, plaid_item=item, plaid_account_id=pid, account_name=name, account_type=typ
    )
    return mk("CHK", "Operating", "depository"), mk("CARD", "Visa", "credit")


def tx(user, amount, account, day, category=None, gaap="operating_expense", **kw):
    flags = gaap_flags(gaap)
    base = dict(
        user=user, amount=Decimal(amount), plaid_account_id=account, transaction_date=day, source="plaid", merchant_name="m", **flags
    )
    if category:
        base["user_category"] = Category.objects.filter(user__isnull=True, name=category).first()
    return Transaction.objects.create(**{**base, **kw})


def balance(user, name):
    """Debits less credits on the named account."""
    s = JournalLine.objects.filter(entry__user=user, account__name=name).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (s["d"] or 0) - (s["c"] or 0)


def test_entries_are_balanced_and_hit_the_right_accounts(user_a, banks):
    tx(user_a, "-40.00", "CHK", D(2026, 1, 5))  # expense paid from checking
    tx(user_a, "100.00", "CHK", D(2026, 1, 6), gaap="revenue")  # income into checking
    tx(user_a, "-25.00", "CARD", D(2026, 1, 7))  # card purchase: the card is a liability, credited
    r = backfill(user_a, apply=True)

    assert r["mapped"] == 3 and r["balanced"] and r["total_debit"] == r["total_credit"] == Decimal("165.00")
    for e in JournalEntry.objects.filter(user=user_a):
        s = e.lines.aggregate(d=Sum("debit"), c=Sum("credit"))
        assert s["d"] == s["c"]
    assert balance(user_a, "Operating") == Decimal("60.00")  # +100 - 40
    assert balance(user_a, "Visa") == Decimal("-25.00")  # credit balance = owed
    assert Account.objects.get(user=user_a, name="Visa").kind == "liability"
    assert Account.objects.get(user=user_a, name="Operating").kind == "asset"
    assert balance(user_a, "Uncategorized expense") == Decimal("65.00")
    assert balance(user_a, "Uncategorized income") == Decimal("-100.00")


def test_a_category_is_its_own_account(user_a, banks):
    tx(user_a, "-12.00", "CHK", D(2026, 1, 5), category="Internal Transfer", gaap="operating_expense")
    cat = Category.objects.filter(user__isnull=True, type="operating_expense").first()
    t = tx(user_a, "-8.00", "CHK", D(2026, 1, 6))
    t.user_category = cat
    t.save()
    backfill(user_a, apply=True)
    acct = Account.objects.get(user=user_a, category=cat)
    assert acct.kind == "expense" and acct.code.startswith("5") and balance(user_a, cat.name) == Decimal("8.00")


def test_both_sides_of_a_transfer_net_to_zero_and_a_missing_side_shows(user_a, banks):
    tx(user_a, "-500.00", "CHK", D(2026, 1, 22), gaap="transfer")
    tx(user_a, "500.00", "CARD", D(2026, 1, 23), gaap="transfer")
    r = backfill(user_a, apply=True)
    assert r["transfers_in_transit"] == 0 and balance(user_a, "Transfers in transit") == 0
    assert balance(user_a, "Visa") == Decimal("500.00")  # paying the card reduces what is owed (debit)

    tx(user_a, "-70.00", "CHK", D(2026, 2, 1), gaap="transfer")  # other side never arrived
    assert backfill(user_a, apply=False)["transfers_in_transit"] == Decimal("70.00")  # money left, nothing arrived


def test_repeating_the_backfill_does_not_duplicate(user_a, banks):
    tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    backfill(user_a, apply=True)
    backfill(user_a, apply=True)
    assert JournalEntry.objects.filter(user=user_a).count() == 1 and JournalLine.objects.filter(entry__user=user_a).count() == 2
    assert Account.objects.filter(user=user_a, name="Operating").count() == 1


def test_dry_run_writes_nothing(user_a, banks):
    tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    JournalEntry.objects.all().delete()  # saving already posted it; start from the "never posted" state
    Account.objects.all().delete()
    r = backfill(user_a)
    assert r["applied"] is False and r["mapped"] == 1
    assert not JournalEntry.objects.exists() and not Account.objects.exists()


def test_rows_that_are_not_posted_are_skipped_and_say_why(user_a, banks):
    tx(user_a, "-1.00", "CHK", D(2026, 1, 1), pending=True)
    tx(user_a, "-2.00", "CHK", D(2026, 1, 2), status="void")
    tx(user_a, "-3.00", "CHK", D(2026, 1, 3), is_hidden=True)
    tx(user_a, "-4.00", "CHK", D(2026, 1, 4), source="other")
    tx(user_a, "-5.00", None, D(2026, 1, 5))
    tx(user_a, "-6.00", "CHK", D(2026, 1, 6))
    r = backfill(user_a, apply=True)
    assert r["mapped"] == 1
    assert r["skipped"] == {"pending": 1, "void": 1, "hidden": 1, "source:other": 1, "no_bank_account": 1}
    assert JournalEntry.objects.filter(user=user_a).count() == 1


def test_a_row_that_stops_counting_loses_its_entry(user_a, banks):
    t = tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    backfill(user_a, apply=True)
    assert JournalEntry.objects.filter(transaction=t).exists()
    Transaction.objects.filter(pk=t.pk).update(status="void")
    backfill(user_a, apply=True)
    assert not JournalEntry.objects.filter(transaction=t).exists()


def test_project_follows_income_and_expense_lines_only(user_a, banks):
    from apps.ledger.models import Project

    p = Project.objects.create(user=user_a, name="Job 1")
    tx(user_a, "-40.00", "CHK", D(2026, 1, 5), project=p)
    backfill(user_a, apply=True)
    by_acct = {ln.account.name: ln.project_id for ln in JournalLine.objects.filter(entry__user=user_a).select_related("account")}
    assert by_acct["Uncategorized expense"] == p.id and by_acct["Operating"] is None


def test_other_users_data_is_untouched(user_a, banks):
    from tests.conftest import make_user

    b = make_user("b@example.com")
    JournalEntry.objects.all().delete()
    tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    JournalEntry.objects.all().delete()
    backfill(b, apply=True)
    assert not JournalEntry.objects.exists()


def ghl(user, ref, amount, gaap, day=D(2026, 6, 1), category=None, **kw):
    base = dict(user=user, amount=Decimal(amount), transaction_date=day, source="ghl", external_ref=ref, **gaap_flags(gaap))
    if category:
        base["user_category"] = Category.objects.filter(user__isnull=True, name=category).first()
    return Transaction.objects.create(**{**base, **kw})


def test_ghl_invoice_posts_receivable_revenue_tax_and_payment(user_a):
    ghl(user_a, "ghl:inv:1:revenue", "100.00", "revenue", category="Other Income")
    ghl(user_a, "ghl:inv:1:tax", "3.00", "liability", category="Sales Tax Payable")
    ghl(user_a, "ghl:inv:1:ar", "0.00", "asset", accounting_status="received")
    ghl(user_a, "ghl:pay:p1", "103.00", "asset", D(2026, 6, 2), accounting_status="received")
    ghl(user_a, "ghl:fee:p1", "-3.30", "operating_expense", D(2026, 6, 2))
    r = backfill(user_a, apply=True)
    assert r["balanced"] and r["mapped"] == 4
    assert balance(user_a, "Accounts receivable") == 0  # billed 103, paid 103
    assert balance(user_a, "Collected through GoHighLevel, not yet in a bank account") == Decimal("99.70")
    assert balance(user_a, "Other Income") == Decimal("-100.00")
    assert balance(user_a, "Sales Tax Payable") == Decimal("-3.00")
    assert balance(user_a, "Uncategorized expense") == Decimal("3.30")
    assert r["receivable"]["difference"] == 0


def test_ghl_open_invoice_matches_its_balance_due_row(user_a):
    ghl(user_a, "ghl:inv:2:revenue", "100.00", "revenue")
    ghl(user_a, "ghl:inv:2:ar", "100.00", "asset", accounting_status="accounts_receivable")
    r = backfill(user_a)
    assert r["skipped"] == {"ghl_balance_view": 1}
    assert r["receivable"] == {"books": Decimal("100.00"), "open_invoices": Decimal("100.00"), "difference": 0}


def test_missing_tax_row_shows_up_in_the_receivable_check(user_a):
    ghl(user_a, "ghl:inv:3:revenue", "100.00", "revenue")
    ghl(user_a, "ghl:inv:3:ar", "0.00", "asset", accounting_status="received")
    ghl(user_a, "ghl:pay:p3", "103.00", "asset")
    r = backfill(user_a)
    assert r["receivable"]["difference"] == Decimal("-3.00")


def test_ghl_refund_reverses_revenue_and_void_invoices_are_skipped(user_a):
    ghl(user_a, "ghl:refund:p1", "-20.00", "revenue", category="Other Income")
    ghl(user_a, "ghl:inv:4:revenue", "50.00", "revenue", status="void")
    r = backfill(user_a, apply=True)
    assert r["mapped"] == 1 and r["skipped"] == {"void": 1}
    assert balance(user_a, "Other Income") == Decimal("20.00")
    assert balance(user_a, "Collected through GoHighLevel, not yet in a bank account") == Decimal("-20.00")


def test_manual_entries_use_receivable_payable_or_cash(user_a):
    man = lambda amt, gaap, **kw: Transaction.objects.create(  # noqa: E731
        user=user_a, amount=Decimal(amt), transaction_date=D(2026, 11, 6), source="manual", **gaap_flags(gaap), **kw
    )
    man("-100.00", "cogs", accounting_status="accounts_payable")
    man("-50.00", "operating_expense")
    man("200.00", "revenue", accounting_status="accounts_receivable")
    r = backfill(user_a, apply=True)
    assert r["balanced"] and r["mapped"] == 3 and not r["skipped"]
    assert balance(user_a, "Accounts payable") == Decimal("-100.00")
    assert balance(user_a, "Cash - manual entries (no bank chosen)") == Decimal("-50.00")
    assert balance(user_a, "Accounts receivable") == Decimal("200.00")
    assert r["receivable"]["difference"] == 0


def test_row_from_an_unknown_bank_account_posts_to_a_placeholder(user_a, banks):
    tx(user_a, "-9.00", "GONE-ACCOUNT-123456", D(2026, 2, 1))
    r = backfill(user_a, apply=True)
    assert r["mapped"] == 1 and not r["skipped"] and r["balanced"]
    assert balance(user_a, "Bank account not on file (123456)") == Decimal("-9.00")


# ---- posting on every write: nobody has to run the backfill to keep the books current
def test_saving_a_transaction_posts_it(user_a, banks):
    t = tx(user_a, "-40.00", "CHK", D(2026, 1, 5), category="Office Supplies & Expenses")
    assert JournalEntry.objects.filter(transaction=t).count() == 1
    assert balance(user_a, "Office Supplies & Expenses") == Decimal("40.00")


def test_editing_the_amount_or_category_rewrites_the_entry(user_a, banks):
    t = tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    t.amount = Decimal("-55.00")
    t.save()
    assert JournalEntry.objects.filter(transaction=t).count() == 1
    assert JournalLine.objects.filter(entry__transaction=t).aggregate(d=Sum("debit"))["d"] == Decimal("55.00")


def test_deleting_a_transaction_removes_its_entry(user_a, banks):
    t = tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    t.delete()
    assert not JournalEntry.objects.filter(user=user_a).exists()


def test_bulk_update_helper_reposts_changed_rows(user_a, banks):
    from apps.ledger.services.journal import update_and_repost

    t = tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    cat = Category.objects.get(user__isnull=True, name="Office Supplies & Expenses")
    n = update_and_repost(Transaction.objects.filter(pk=t.pk), user_category=cat)
    assert n == 1
    assert JournalLine.objects.filter(entry__transaction=t, account__category=cat).exists()


def test_a_posting_failure_never_blocks_saving_the_transaction(user_a, banks, monkeypatch):
    from apps.ledger.services import journal

    monkeypatch.setattr(journal, "post_transaction", lambda *a, **k: 1 / 0)
    t = tx(user_a, "-40.00", "CHK", D(2026, 1, 5))
    assert Transaction.objects.filter(pk=t.pk).exists() and not JournalEntry.objects.exists()


# ---- opening balances
def test_opening_balance_makes_books_match_the_bank(user_a, banks):
    from apps.ledger.services.journal import opening_balances

    chk, card = banks
    chk.current_balance, card.current_balance = Decimal("1000.00"), Decimal("200.00")
    chk.save()
    card.save()
    tx(user_a, "300.00", "CHK", D(2026, 1, 10), gaap="revenue")  # bank holds 1000 now, so it held 700 before
    tx(user_a, "-50.00", "CARD", D(2026, 1, 11))  # card owes 200 now; the 50 is part of it
    rows = opening_balances(user_a)
    assert not JournalEntry.objects.filter(kind="opening").exists()  # dry run
    assert {r["account"]: r["opening"] for r in rows} == {"Operating": Decimal("700.00"), "Visa": Decimal("150.00")}

    opening_balances(user_a, apply=True)
    assert balance(user_a, "Operating") == Decimal("1000.00")  # asset: debits less credits
    assert balance(user_a, "Visa") == Decimal("-200.00")  # liability: credits exceed debits
    assert balance(user_a, "Opening balance equity") == Decimal("-550.00")  # 700 in, 150 owed out
    assert JournalEntry.objects.get(kind="opening", memo__contains="Operating").date == D(2026, 1, 9)
    total = JournalLine.objects.filter(entry__user=user_a).aggregate(d=Sum("debit"), c=Sum("credit"))
    assert total["d"] == total["c"]


def test_opening_balance_is_replaced_not_stacked(user_a, banks):
    from apps.ledger.services.journal import opening_balances

    chk, _ = banks
    chk.current_balance = Decimal("500.00")
    chk.save()
    tx(user_a, "100.00", "CHK", D(2026, 1, 10), gaap="revenue")
    opening_balances(user_a, apply=True)
    opening_balances(user_a, apply=True)
    assert JournalEntry.objects.filter(kind="opening").count() == 1
    assert balance(user_a, "Operating") == Decimal("500.00")


def test_accounts_without_a_bank_balance_are_skipped(user_a, banks):
    from apps.ledger.services.journal import opening_balances

    rows = opening_balances(user_a, apply=True)
    assert all("skipped" in r for r in rows) and not JournalEntry.objects.exists()


def test_row_type_wins_over_a_category_of_the_opposite_kind(user_a):
    ghl(user_a, "ghl:inv:9:revenue", "100.00", "revenue", category="Cost of Goods Sold")
    backfill(user_a, apply=True)
    assert balance(user_a, "Uncategorized income") == Decimal("-100.00")  # credited as income, not a negative expense
    assert not Account.objects.filter(name="Cost of Goods Sold", kind="expense", lines__isnull=False).exists()


def test_uncategorized_equity_stays_off_the_profit_report(user_a):
    Transaction.objects.create(
        user=user_a,
        amount=Decimal("-500.00"),
        transaction_date=D(2026, 5, 1),
        source="manual",
        description="Owner draw",
        **gaap_flags("equity"),
    )
    backfill(user_a, apply=True)
    assert balance(user_a, "Uncategorized equity") == Decimal("500.00")
    assert not Account.objects.filter(kind__in=("income", "expense")).exists()
