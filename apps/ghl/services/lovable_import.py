"""Import one location's data exported from the old (Lovable/Supabase) app. See tools/export_lovable_location.py.

Safe to run repeatedly: every record keeps its old UUID as its primary key, so a second run adds nothing. Bank access tokens
are never part of the export; imported bank connections are placeholders (status "imported") until the bank is reconnected,
at which point apps.banking.services.adopt merges the history with the fresh sync.
"""

from collections import Counter
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils.dateparse import parse_date, parse_datetime

from apps.accounts.models import User
from apps.banking.models import BankAccount, PlaidItem
from apps.ledger.models import Category, Ledger, Transaction

from ..models import GhlInvoice, GhlInvoiceEvent, GhlInvoicePayment, GhlLocation
from .linking import TENANT_DOMAIN, is_tenant

IMPORTED = "imported"  # PlaidItem.connection_status of a placeholder awaiting reconnection


class ImportError_(Exception):
    pass


def norm(text) -> str:
    return " ".join((text or "").lower().split())


def tx_key(date, amount, desc) -> tuple:
    return (str(date), Decimal(str(amount)).quantize(Decimal("0.01")), norm(desc))


def _dt(v):
    return parse_datetime(v) if v else None


def _d(v):
    return parse_date(v) if v else None


def target_user(data: dict, location_id: str) -> tuple[User, GhlLocation, bool]:
    """The workspace that owns this location in the new app (created like the embedded sign-in would if missing)."""
    if data["location"]["location_id"] != location_id:
        raise ImportError_("The export file is for a different location.")
    loc = GhlLocation.objects.filter(location_id=location_id).first()
    created = False
    if loc is None:
        loc = GhlLocation.objects.create(location_id=location_id, name=data["location"].get("label"))
        created = True
    if loc.user_id is None:
        user = User.objects.create_user(f"ghl-{location_id.lower()}{TENANT_DOMAIN}", None, full_name=f"GHL {location_id}")
        loc.user = user
        loc.save(update_fields=["user"])
        created = True
    return loc.user, loc, created


def import_location(data: dict, location_id: str, *, dry_run: bool = False) -> dict:
    stats: Counter = Counter()
    with db_transaction.atomic():
        user, loc, created = target_user(data, location_id)
        stats["workspace_created"] = int(created)

        # ---- categories: re-link by (name, type) to the shared chart of accounts
        cat_map: dict = {}
        for c in data.get("categories", []):
            match = (
                Category.objects.filter(name=c["name"], type=c["type"]).filter(user__isnull=True).first()
                or Category.objects.filter(name=c["name"], type=c["type"], user=user).first()
            )
            if match is None:
                match = Category.objects.create(user=user, name=c["name"], type=c["type"], is_system=False)
                stats["categories_created"] += 1
            cat_map[c["id"]] = match

        # ---- ledgers: same label (or same GHL contact) in this workspace is the same ledger
        ledger_map: dict = {}
        for row in data.get("ledgers", []):
            existing = (
                Ledger.objects.for_user(user).filter(id=row["id"]).first()
                or Ledger.objects.for_user(user).filter(label=row["label"]).first()
            )
            if existing is None and row.get("ghl_contact_id"):
                existing = Ledger.objects.for_user(user).filter(ghl_contact_id=row["ghl_contact_id"]).first()
            if existing is None:
                existing = Ledger.objects.create(
                    id=row["id"],
                    user=user,
                    label=row["label"],
                    contact_name=row.get("contact_name"),
                    contact_email=row.get("contact_email"),
                    contact_phone=row.get("contact_phone"),
                    notes=row.get("notes"),
                    account_class=row["account_class"],
                    ghl_contact_id=row.get("ghl_contact_id"),
                )
                stats["ledgers_created"] += 1
            else:
                stats["ledgers_matched"] += 1
            ledger_map[row["id"]] = existing

        # ---- bank connections: reuse a live connection of the same bank, else a token-less placeholder
        item_map: dict = {}
        account_map: dict = {}  # old plaid_account_id -> BankAccount
        for it in data.get("plaid_items", []):
            live = (
                PlaidItem.objects.for_user(user)
                .filter(institution_id=it["institution_id"])
                .exclude(connection_status=IMPORTED)
                .exclude(access_token_encrypted="")
                .first()
                if it.get("institution_id")
                else None
            )
            existing_item = PlaidItem.objects.filter(item_id=it["item_id"]).first()
            if existing_item is not None and existing_item.user_id != user.id:
                raise ImportError_(f"Bank connection {it['item_id']} already belongs to another workspace.")
            item = live or existing_item
            if item is None:
                item = PlaidItem.objects.create(
                    id=it["id"],
                    user=user,
                    item_id=it["item_id"],
                    institution_id=it.get("institution_id"),
                    institution_name=it.get("institution_name"),
                    access_token_encrypted="",
                    environment=it.get("environment") or "production",
                    connection_status=IMPORTED,
                    error_message="History imported from the previous app. Reconnect this bank to resume syncing.",
                    initial_sync_done=True,
                    last_sync_at=_dt(it.get("last_sync_at")),
                )
                stats["bank_connections_created"] += 1
            item_map[it["id"]] = item
        for acc in data.get("bank_accounts", []):
            item = item_map.get(acc["plaid_item_id"])
            if item is None:
                continue
            existing = BankAccount.objects.filter(plaid_account_id=acc["plaid_account_id"]).first()
            if existing is None and item.connection_status != IMPORTED:  # live connection: match its account by type + last digits
                existing = BankAccount.objects.filter(plaid_item=item, mask=acc.get("mask"), account_type=acc.get("account_type")).first()
            if existing is None:
                existing = BankAccount.objects.create(
                    id=acc["id"],
                    user=user,
                    plaid_item=item,
                    plaid_account_id=acc["plaid_account_id"],
                    institution_id=acc.get("institution_id"),
                    account_name=acc.get("account_name"),
                    official_name=acc.get("official_name"),
                    mask=acc.get("mask"),
                    account_type=acc.get("account_type"),
                    account_subtype=acc.get("account_subtype"),
                    current_balance=acc.get("current_balance"),
                    available_balance=acc.get("available_balance"),
                    iso_currency_code=acc.get("iso_currency_code") or "USD",
                    last_balance_update=_dt(acc.get("last_balance_update")),
                )
                stats["bank_accounts_created"] += 1
            elif existing.user_id != user.id:
                raise ImportError_(f"Bank account {acc['plaid_account_id']} already belongs to another workspace.")
            account_map[acc["plaid_account_id"]] = existing

        # ---- transactions
        # Rows already in the workspace from a live bank sync: an identical date/amount/description is the same transaction,
        # so it is skipped (counted once per existing row, so genuine same-day twins in the old data still import).
        live_keys: Counter = Counter(
            tx_key(t.transaction_date, t.amount, t.original_description or t.description or t.merchant_name)
            for t in Transaction.objects.for_user(user).filter(source="plaid", removed_at__isnull=True)
        )
        existing_ids = set(Transaction.objects.filter(id__in=[r["id"] for r in data.get("transactions", [])]).values_list("id", flat=True))
        existing_ids = {str(i) for i in existing_ids}
        existing_refs = set(Transaction.objects.for_user(user).filter(external_ref__isnull=False).values_list("external_ref", flat=True))
        existing_plaid = set(
            Transaction.objects.filter(
                plaid_transaction_id__in=[r["plaid_transaction_id"] for r in data.get("transactions", []) if r.get("plaid_transaction_id")]
            ).values_list("plaid_transaction_id", flat=True)
        )
        batch = []
        for r in data.get("transactions", []):
            if r.get("removed_at"):
                stats["transactions_skipped_removed"] += 1
                continue
            if r["id"] in existing_ids:
                stats["transactions_already_imported"] += 1
                continue
            if r.get("external_ref") and r["external_ref"] in existing_refs:
                stats["transactions_duplicate_ref"] += 1
                continue
            if r.get("plaid_transaction_id") and r["plaid_transaction_id"] in existing_plaid:
                stats["transactions_duplicate_plaid_id"] += 1
                continue
            if r["source"] == "plaid":
                key = tx_key(
                    r["transaction_date"], r["amount"], r.get("original_description") or r.get("description") or r.get("merchant_name")
                )
                if live_keys[key] > 0:
                    live_keys[key] -= 1
                    stats["transactions_duplicate_of_live_sync"] += 1
                    continue
            account = account_map.get(r.get("plaid_account_id"))
            item = item_map.get(r.get("plaid_item_id"))
            cat = cat_map.get(r.get("user_category_id"))
            batch.append(
                Transaction(
                    id=r["id"],
                    user=user,
                    plaid_item=item,
                    plaid_account_id=account.plaid_account_id if account else r.get("plaid_account_id"),
                    plaid_transaction_id=r.get("plaid_transaction_id"),
                    ledger=ledger_map.get(r.get("ledger_id")),
                    merchant_name=r.get("merchant_name"),
                    original_description=r.get("original_description"),
                    description=r.get("description"),
                    amount=Decimal(str(r["amount"])),
                    currency=r.get("currency") or "USD",
                    transaction_date=_d(r["transaction_date"]),
                    authorized_date=_d(r.get("authorized_date")),
                    due_date=_d(r.get("due_date")),
                    pending=bool(r.get("pending")),
                    payment_channel=r.get("payment_channel"),
                    transaction_type=r.get("transaction_type"),
                    merchant_entity_id=r.get("merchant_entity_id"),
                    merchant_logo_url=r.get("merchant_logo_url"),
                    website=r.get("website"),
                    city=r.get("city"),
                    region=r.get("region"),
                    postal_code=r.get("postal_code"),
                    country=r.get("country"),
                    plaid_personal_finance_primary_category=r.get("plaid_personal_finance_primary_category"),
                    plaid_personal_finance_detailed_category=r.get("plaid_personal_finance_detailed_category"),
                    plaid_category_confidence=r.get("plaid_category_confidence"),
                    user_category=cat,
                    category_source=r.get("category_source"),
                    categorization_confidence=r.get("categorization_confidence"),
                    transaction_direction=r.get("transaction_direction"),
                    gaap_type=r.get("gaap_type"),
                    is_income=bool(r.get("is_income")),
                    is_expense=bool(r.get("is_expense")),
                    is_transfer=bool(r.get("is_transfer")),
                    is_recurring=bool(r.get("is_recurring")),
                    is_hidden=bool(r.get("is_hidden")),
                    is_demo=bool(r.get("is_demo")),
                    source=r["source"],
                    status=r.get("status") or "posted",
                    accounting_status=r.get("accounting_status"),
                    external_ref=r.get("external_ref"),
                )
            )
            stats["transactions_created"] += 1
        Transaction.objects.bulk_create(batch, batch_size=500)

        # ---- GHL invoices, payments and their audit events
        invoice_map: dict = {}
        for inv in data.get("ghl_invoices", []):
            existing = GhlInvoice.objects.filter(user=user, ghl_invoice_id=inv["ghl_invoice_id"]).first()
            if existing:
                invoice_map[inv["id"]] = existing
                stats["invoices_already_present"] += 1
                continue
            invoice_map[inv["id"]] = GhlInvoice.objects.create(
                id=inv["id"],
                user=user,
                location_id=location_id,
                ghl_invoice_id=inv["ghl_invoice_id"],
                ghl_contact_id=inv.get("ghl_contact_id"),
                ledger=ledger_map.get(inv.get("ledger_id")),
                invoice_number=inv.get("invoice_number"),
                issue_date=_d(inv.get("issue_date")),
                due_date=_d(inv.get("due_date")),
                currency=inv.get("currency"),
                subtotal=inv.get("subtotal") or 0,
                discount=inv.get("discount") or 0,
                tax=inv.get("tax") or 0,
                total=inv.get("total") or 0,
                amount_paid=inv.get("amount_paid") or 0,
                amount_refunded=inv.get("amount_refunded") or 0,
                balance=inv.get("balance") or 0,
                ghl_status=inv.get("ghl_status"),
                status=inv.get("status") or "open",
                needs_review=bool(inv.get("needs_review")),
                review_reason=inv.get("review_reason"),
                raw=inv.get("raw") or {},
            )
            stats["invoices_created"] += 1
        new_invoice_ids = {old for old, obj in invoice_map.items() if str(obj.id) == old}
        for p in data.get("ghl_invoice_payments", []):
            inv = invoice_map.get(p["invoice_id"])
            if inv is None or GhlInvoicePayment.objects.filter(user=user, ghl_transaction_id=p["ghl_transaction_id"]).exists():
                continue
            GhlInvoicePayment.objects.create(
                id=p["id"],
                user=user,
                invoice=inv,
                ghl_transaction_id=p["ghl_transaction_id"],
                amount=p.get("amount") or 0,
                amount_refunded=p.get("amount_refunded") or 0,
                fee=p.get("fee") or 0,
                status=p.get("status"),
                paid_at=_dt(p.get("paid_at")),
                reference=p.get("reference"),
                provider=p.get("provider"),
                raw=p.get("raw") or {},
            )
            stats["invoice_payments_created"] += 1
        for e in data.get("ghl_invoice_events", []):
            if e["invoice_id"] not in new_invoice_ids or GhlInvoiceEvent.objects.filter(id=e["id"]).exists():
                continue  # history is only brought in together with a newly created invoice
            GhlInvoiceEvent.objects.create(
                id=e["id"],
                user=user,
                invoice=invoice_map[e["invoice_id"]],
                ghl_invoice_id=e["ghl_invoice_id"],
                source=e["source"],
                event_type=e.get("event_type"),
                previous_status=e.get("previous_status"),
                new_status=e.get("new_status"),
                previous_balance=e.get("previous_balance"),
                new_balance=e.get("new_balance"),
                payload=e.get("payload") or {},
            )
            stats["invoice_events_created"] += 1

        stats["workspace_user"] = user.email
        stats["workspace_is_tenant"] = int(is_tenant(user))
        if dry_run:
            db_transaction.set_rollback(True)
    return dict(stats)
