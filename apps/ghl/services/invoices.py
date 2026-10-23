"""GoHighLevel invoice import (port of src/lib/ghl-invoices.server.ts). Accrual-basis, idempotent."""

import logging
import math
from datetime import date
from decimal import Decimal

import httpx
from django.utils import timezone

from apps.ledger.models import Ledger, Transaction
from apps.ledger.services.gaap import gaap_flags

from ..models import GhlInvoice, GhlInvoiceEvent, GhlInvoicePayment, GhlLocation
from .oauth import API, get_location_access_token

logger = logging.getLogger(__name__)


def ghl_get(location_id: str, path: str) -> dict:
    token = get_location_access_token(location_id)
    res = httpx.get(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}", "Version": "2021-07-28", "Accept": "application/json"},
        timeout=60,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"GoHighLevel {path.split('?')[0]} failed ({res.status_code})")
    return res.json()


def n(v) -> float:
    """Port of the JS helper: finite number rounded half-up to cents, else 0."""
    try:
        x = float(0 if v is None else v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x):
        return 0.0
    return math.floor(x * 100 + 0.5) / 100


def day(v) -> str | None:
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def _dec(v: float) -> Decimal:
    return Decimal(str(round(v, 2)))


def compute_invoice(inv: dict, payments: list[dict] | None = None) -> dict:
    """Pure mapping of a GoHighLevel invoice to accounting amounts and status (accrual basis)."""
    payments = payments or []
    summary = inv.get("totalSummary") or {}
    items = inv.get("invoiceItems")
    items_sum = sum(n(i.get("amount")) * n(i.get("qty") or 1) for i in items) if isinstance(items, list) else None
    subtotal = n(summary["subTotal"] if summary.get("subTotal") is not None else items_sum)
    discount = n(summary.get("discount") if summary.get("discount") is not None else 0)
    total_raw = (
        inv.get("total") if inv.get("total") is not None else (inv.get("invoiceTotal") if inv.get("invoiceTotal") is not None else 0)
    )
    total = n(total_raw)
    tax = n(summary["tax"] if summary.get("tax") is not None else max(0, total - (subtotal - discount)))
    amount_paid = n(inv.get("amountPaid") or 0)
    refunded = n(sum(p["refunded"] for p in payments))
    ghl_status = str(inv.get("status") or "")
    voided = ghl_status in ("void", "cancelled", "canceled")
    balance = 0.0 if voided else n(max(0, total - amount_paid))
    reasons = []
    if inv.get("currency") and inv["currency"] != "USD":
        reasons.append(f"Currency {inv['currency']} needs conversion review")
    amount_due = inv.get("amountDue") if inv.get("amountDue") is not None else balance
    if not voided and abs(n(amount_due) - balance) > 0.009:
        reasons.append("GoHighLevel amount due does not match total less payments")
    if abs(subtotal - discount + tax - total) > 0.009:
        reasons.append("Subtotal, discount and tax do not add up to the total")
    paid_sum = n(sum(p["amount"] for p in payments))
    if payments and abs(paid_sum - amount_paid) > 0.009:
        reasons.append("Payment records do not match amount paid")
    if refunded > 0:
        reasons.append("Refund recorded — review revenue reversal")
    if ghl_status in ("disputed", "chargeback"):
        reasons.append("Payment disputed")
    due = day(inv.get("dueDate"))
    if voided:
        status = "void"
    elif refunded > 0 and refunded >= amount_paid and amount_paid > 0:
        status = "refunded"
    elif balance == 0 and total > 0:
        status = "paid"
    elif amount_paid > 0:
        status = "partially_paid"
    elif due and due < date.today().isoformat():
        status = "overdue"
    else:
        status = "open"
    return {
        "subtotal": subtotal,
        "discount": discount,
        "tax": tax,
        "total": total,
        "amount_paid": amount_paid,
        "refunded": refunded,
        "balance": balance,
        "status": status,
        "ghl_status": ghl_status,
        "reasons": reasons,
        "revenue": n(subtotal - discount),
    }


def ensure_ledger(user, contact: dict | None) -> Ledger | None:
    contact_id = (contact or {}).get("id")
    if not contact_id:
        return None
    found = Ledger.objects.for_user(user).filter(ghl_contact_id=contact_id).first()
    if found:
        return found
    base = str(contact.get("companyName") or contact.get("name") or contact.get("email") or f"Customer {contact_id}")[:100]
    for label in (base, f"{base} ({contact_id[:6]})", f"{base} ({contact_id})"):
        if Ledger.objects.for_user(user).filter(label=label).exists():
            continue
        return Ledger.objects.create(
            user=user,
            label=label,
            account_class="current_asset",
            ghl_contact_id=contact_id,
            contact_name=contact.get("name"),
            contact_email=contact.get("email"),
            contact_phone=contact.get("phoneNo"),
            notes="Customer account created from GoHighLevel invoices.",
        )
    raise RuntimeError("Could not create customer ledger")


def _post(user, ref: str, type_: str, amount: float, **extra) -> None:
    Transaction.objects.update_or_create(
        user=user,
        external_ref=ref,
        defaults={
            "source": "ghl",
            "currency": "USD",
            "amount": _dec(amount),
            "category_source": "user_rule",
            "categorization_confidence": "high",
            "transaction_direction": "inflow" if amount >= 0 else "outflow",
            "pending": False,
            "status": "posted",
            "removed_at": None,
            **gaap_flags(type_),
            **extra,
        },
    )


def sync_invoice(location_id: str, user, invoice_id: str, source: str, event_type: str | None = None, inv: dict | None = None):
    """Re-reads one invoice from GoHighLevel and posts it idempotently."""
    if inv is None:
        j = ghl_get(location_id, f"/invoices/{invoice_id}?altId={location_id}&altType=location")
        inv = j.get("invoice") or j
    if not inv.get("_id") or inv.get("altId") != location_id:
        raise RuntimeError("Invoice not found for this subaccount")
    if inv.get("status") == "draft":
        return {"skipped": True}

    pays: list[dict] = []
    if n(inv.get("amountPaid")) > 0 or inv.get("status") == "refunded":
        try:
            j = ghl_get(location_id, f"/payments/transactions?altId={location_id}&altType=location&entityId={inv['_id']}&limit=100")
        except Exception:  # noqa: BLE001
            j = {"data": []}
        pays = [p for p in j.get("data", []) if p.get("status") in ("succeeded", "refunded", "partially_refunded")]
    c = compute_invoice(inv, [{"amount": n(p.get("amount")), "refunded": n(p.get("amountRefunded"))} for p in pays])
    ledger = ensure_ledger(user, inv.get("contactDetails"))
    number = f"{inv.get('invoiceNumberPrefix') or ''}{inv.get('invoiceNumber') or ''}" or inv["_id"]
    contact = inv.get("contactDetails") or {}

    prev = GhlInvoice.objects.filter(user=user, ghl_invoice_id=inv["_id"]).first()
    saved, _ = GhlInvoice.objects.update_or_create(
        user=user,
        ghl_invoice_id=inv["_id"],
        defaults={
            "location_id": location_id,
            "ghl_contact_id": contact.get("id"),
            "ledger": ledger,
            "invoice_number": number,
            "issue_date": day(inv.get("issueDate")),
            "due_date": day(inv.get("dueDate")),
            "currency": inv.get("currency") or "USD",
            "subtotal": _dec(c["subtotal"]),
            "discount": _dec(c["discount"]),
            "tax": _dec(c["tax"]),
            "total": _dec(c["total"]),
            "amount_paid": _dec(c["amount_paid"]),
            "amount_refunded": _dec(c["refunded"]),
            "balance": _dec(c["balance"]),
            "ghl_status": c["ghl_status"],
            "status": c["status"],
            "needs_review": bool(c["reasons"]),
            "review_reason": "; ".join(c["reasons"]) or None,
            "raw": inv,
        },
    )

    for p in pays:
        GhlInvoicePayment.objects.update_or_create(
            user=user,
            ghl_transaction_id=p["_id"],
            defaults={
                "invoice": saved,
                "amount": _dec(n(p.get("amount"))),
                "amount_refunded": _dec(n(p.get("amountRefunded"))),
                "fee": _dec(n(p.get("fee") or (p.get("chargeSnapshot") or {}).get("fee") or p.get("applicationFee") or 0)),
                "status": p.get("status"),
                "paid_at": p.get("fulfilledAt") or p.get("createdAt"),
                "reference": p.get("chargeId"),
                "provider": (p.get("customProvider") or {}).get("name") or p.get("paymentProviderType"),
                "raw": p,
            },
        )

    # Accrual postings: revenue and sales tax at issue; the customer ledger carries only the open balance.
    issue = day(inv.get("issueDate")) or day(inv.get("createdAt"))
    base = {"transaction_date": issue, "status": "void" if c["status"] == "void" else "posted"}
    name = contact.get("name")
    _post(
        user,
        f"ghl:inv:{inv['_id']}:revenue",
        "revenue",
        c["revenue"],
        **base,
        ledger=None,
        description=f"Invoice {number} — revenue",
        merchant_name=name,
    )
    _post(
        user,
        f"ghl:inv:{inv['_id']}:ar",
        "asset",
        c["balance"],
        **base,
        ledger=ledger,
        due_date=day(inv.get("dueDate")),
        accounting_status="accounts_receivable" if c["balance"] > 0 else "received",
        description=f"Invoice {number} — balance due",
        merchant_name=name,
    )
    if c["tax"] > 0:
        _post(
            user,
            f"ghl:inv:{inv['_id']}:tax",
            "liability",
            c["tax"],
            **base,
            ledger=None,
            description=f"Invoice {number} — sales tax payable",
        )
    for p in pays:
        paid_on = day(p.get("fulfilledAt") or p.get("createdAt")) or issue
        _post(
            user,
            f"ghl:pay:{p['_id']}",
            "asset",
            n(p.get("amount")),
            transaction_date=paid_on,
            ledger=None,
            accounting_status="received",
            description=f"Payment received — invoice {number}",
            merchant_name=name,
        )
        fee = n(p.get("fee") or (p.get("chargeSnapshot") or {}).get("fee") or 0)
        if fee > 0:
            _post(
                user,
                f"ghl:fee:{p['_id']}",
                "operating_expense",
                -fee,
                transaction_date=paid_on,
                ledger=None,
                accounting_status="paid",
                description=f"Payment processing fee — invoice {number}",
            )
        if n(p.get("amountRefunded")) > 0:
            _post(
                user,
                f"ghl:refund:{p['_id']}",
                "revenue",
                -n(p.get("amountRefunded")),
                transaction_date=day(p.get("updatedAt")) or paid_on,
                ledger=None,
                accounting_status="paid",
                description=f"Refund — invoice {number}",
            )

    if not prev or prev.status != c["status"] or float(prev.balance) != c["balance"]:
        GhlInvoiceEvent.objects.create(
            user=user,
            invoice=saved,
            ghl_invoice_id=inv["_id"],
            source=source,
            event_type=event_type,
            previous_status=prev.status if prev else None,
            new_status=c["status"],
            previous_balance=prev.balance if prev else None,
            new_balance=_dec(c["balance"]),
            payload={"ghl_status": c["ghl_status"], "total": c["total"], "amount_paid": c["amount_paid"]},
        )
    return {"status": c["status"]}


def sync_location_invoices(location_id: str, user) -> dict:
    """Full import / reconciliation of every invoice in a subaccount."""
    offset = synced = failed = 0
    while True:
        j = ghl_get(location_id, f"/invoices/?altId={location_id}&altType=location&limit=100&offset={offset}")
        invoices = j.get("invoices") or []
        for inv in invoices:
            try:
                sync_invoice(location_id, user, inv["_id"], "import", None, inv)
                synced += 1
            except Exception:  # noqa: BLE001
                failed += 1
                logger.exception("Invoice sync failed for %s", inv.get("_id"))
        offset += len(invoices)
        if len(invoices) < 100 or offset >= int(j.get("total") or 0):
            break
    GhlLocation.objects.filter(location_id=location_id).update(invoices_synced_at=timezone.now())
    return {"synced": synced, "failed": failed}


def user_for_location(location_id: str):
    loc = GhlLocation.objects.filter(location_id=location_id).select_related("user").first()
    return loc.user if loc and loc.active and loc.user_id else None
