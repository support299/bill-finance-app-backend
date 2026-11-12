import csv
import datetime
import io
import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.banking.models import PlaidItem
from apps.core.crypto import decrypt_secret, encrypt_secret
from apps.core.services import audit

from . import serializers as s
from .models import Attachment, CategoryPosition, CategoryRule, Ledger, LedgerTag, Project, Transaction, VendorTaxProfile
from .services import projects as project_svc
from .services import vendor1099 as v1099
from .services.gaap import accounting_statuses_for_type, gaap_flags, row_gaap_type
from .services.transactions import (
    apply_filters,
    apply_rule_to_existing,
    apply_sort,
    category_positions,
    preview_rule,
    recategorize_all,
    visible_categories,
)
from .services.transfers import link_pair, pair_transfers


def _category_for_user(user, category_id):
    cat = visible_categories(user).filter(pk=category_id).first()
    if not cat:
        raise ValidationError({"category_id": "Category not found."})
    return cat


# ----------------------------------------------------------------- categories
class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = s.CategorySerializer
    pagination_class = None
    filter_backends: list = []

    def get_queryset(self):
        qs = visible_categories(self.request.user)
        # Only a user's own categories are mutable (system ones are shared and read-only).
        if self.action in ("update", "partial_update", "destroy"):
            qs = qs.filter(user=self.request.user, is_system=False)
        return qs.order_by("type", "name")

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request and self.request.user.is_authenticated:
            ctx["positions"] = category_positions(self.request.user)
        return ctx

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, is_system=False)

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """Body {"ids": [...]}: sibling categories (same type, same parent) in the order they should appear on the P&L."""
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids or len({str(i) for i in ids}) != len(ids):
            raise ValidationError({"ids": "Send each category id once, in the order you want."})
        try:
            found = {str(c.id): c for c in visible_categories(request.user).filter(pk__in=ids)}
        except (ValueError, DjangoValidationError):
            raise ValidationError({"ids": "Invalid category id."}) from None
        if len(found) != len(ids):
            raise ValidationError({"ids": "Category not found."})
        if len({(c.type, c.parent_category_id) for c in found.values()}) != 1:
            raise ValidationError({"ids": "Only categories of the same type and parent can be ordered together."})
        for pos, cid in enumerate(ids, start=1):
            CategoryPosition.objects.update_or_create(user=request.user, category=found[str(cid)], defaults={"sort_order": pos * 10})
        return Response({"ok": True})


# ---------------------------------------------------------------------- rules
class RuleViewSet(viewsets.ModelViewSet):
    serializer_class = s.RuleSerializer
    pagination_class = None
    filter_backends: list = []
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return CategoryRule.objects.for_user(self.request.user).select_related("category", "ledger").order_by("priority")

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        apply_existing = ser.validated_data.pop("apply_to_existing", True)
        rule = ser.save(user=request.user)
        applied = apply_rule_to_existing(request.user, rule, rule.category) if apply_existing else 0
        return Response({"rule_id": str(rule.id), "applied": applied}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """What a rule would change among existing transactions, before it is saved."""
        ser = s.RulePreviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        category = _category_for_user(request.user, d["category_id"]) if d.get("category_id") else None
        rule = CategoryRule(user=request.user, keyword=d["keyword"], match_type=d["match_type"], source="user")
        return Response(preview_rule(request.user, rule, category))

    def perform_update(self, serializer):
        serializer.validated_data.pop("apply_to_existing", None)
        serializer.save()


# --------------------------------------------------------------- transactions
class TransactionViewSet(viewsets.GenericViewSet):
    serializer_class = s.TransactionSerializer
    filter_backends: list = []
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        return (
            Transaction.objects.for_user(self.request.user)
            .select_related("user_category", "reconciliation")
            .annotate(attachment_count=Count("attachments"))
        )

    def list(self, request):
        qs = apply_sort(apply_filters(self.get_queryset(), request.query_params), request.query_params)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=False, methods=["get"])
    def export(self, request):
        qs = apply_filters(self.get_queryset(), request.query_params).order_by("-transaction_date")[:10000]
        if request.query_params.get("output") == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(
                ["Date", "Merchant", "Description", "Account", "Amount", "Currency", "Pending", "Income", "Expense", "Transfer", "Category"]
            )
            for t in qs:
                w.writerow(
                    [
                        t.transaction_date,
                        t.merchant_name or "",
                        t.description or "",
                        t.plaid_account_id or "",
                        t.amount,
                        t.currency,
                        t.pending,
                        t.is_income,
                        t.is_expense,
                        t.is_transfer,
                        t.user_category.name if t.user_category else "",
                    ]
                )
            resp = HttpResponse(buf.getvalue(), content_type="text/csv")
            resp["Content-Disposition"] = 'attachment; filename="transactions.csv"'
            return resp
        return Response({"rows": self.get_serializer(qs, many=True).data})

    def partial_update(self, request, pk=None):
        tx = get_object_or_404(self.get_queryset(), pk=pk)
        ser = s.TransactionUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        user = request.user
        if (tx.reconciliation_id and tx.reconciliation.status == "completed") and ("is_hidden" in d or "status" in d):
            raise ValidationError({"detail": "This transaction is part of a completed bank reconciliation. Reopen it to hide or void it."})

        if "category_id" in d:
            tx.user_category_id = d["category_id"]
            tx.category_source = "user_rule"
            tx.categorization_confidence = "high"
            if d["category_id"]:
                cat = _category_for_user(user, d["category_id"])
                self._apply(tx, gaap_flags(cat.type))
        if "classification" in d:
            self._apply(tx, gaap_flags(d["classification"]))
        if "is_hidden" in d:
            tx.is_hidden = d["is_hidden"]
        if "status" in d:
            tx.status = d["status"]
            tx.pending = d["status"] == "pending"
        if "type" in d:
            self._apply(tx, gaap_flags(d["type"]))
        if "ledger_id" in d:
            if d["ledger_id"] and not Ledger.objects.for_user(user).filter(pk=d["ledger_id"]).exists():
                raise ValidationError({"ledger_id": "Ledger account not found."})
            tx.ledger_id = d["ledger_id"]
        if "project_id" in d:
            if d["project_id"] and not Project.objects.for_user(user).filter(pk=d["project_id"]).exists():
                raise ValidationError({"project_id": "Project not found."})
            tx.project_id = d["project_id"]
            if not d["project_id"]:
                tx.project_role = None
        if "project_role" in d:
            tx.project_role = d["project_role"]
        if "due_date" in d:
            tx.due_date = d["due_date"]
        if "accounting_status" in d:
            if not tx.ledger_id:
                raise ValidationError({"accounting_status": "Assign this transaction to a ledger before setting its accounting status."})
            st = d["accounting_status"]
            if st and st not in accounting_statuses_for_type(row_gaap_type(tx)):
                raise ValidationError({"accounting_status": "That accounting status does not match the transaction type."})
            tx.accounting_status = st
        tx.save()
        return Response({"ok": True})

    @staticmethod
    def _apply(tx, flags: dict):
        for k, v in flags.items():
            setattr(tx, k, v)

    def _bulk_targets(self, d):
        """Queryset a bulk action applies to (always the caller's own, non-removed rows)."""
        if d.get("select_all"):
            qs = apply_filters(self.get_queryset(), d["filters"])
            if "expected_count" in d and qs.count() != d["expected_count"]:
                raise ValidationError({"detail": "The matching transactions changed. Reload the list and try again."})
            return qs
        return self.get_queryset().filter(id__in=d["ids"])

    @action(detail=False, methods=["post"], url_path="bulk-categorize")
    def bulk_categorize(self, request):
        ser = s.BulkCategorizeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cat = _category_for_user(request.user, ser.validated_data["category_id"])
        n = self._bulk_targets(ser.validated_data).update(
            user_category=cat, category_source="user_rule", categorization_confidence="high", **gaap_flags(cat.type)
        )
        return Response({"updated": n})

    @action(detail=False, methods=["post"], url_path="bulk-assign-project")
    def bulk_assign_project(self, request):
        ser = s.BulkAssignProjectSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if d["project_id"] and not Project.objects.for_user(request.user).filter(pk=d["project_id"]).exists():
            raise ValidationError({"project_id": "Project not found."})
        changes: dict = {"project_id": d["project_id"]}
        if not d["project_id"]:
            changes["project_role"] = None
        elif "project_role" in d:
            changes["project_role"] = d["project_role"]
        return Response({"updated": self._bulk_targets(d).update(**changes)})

    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        """Permanently delete transactions.

        Bank rows are deleted too: the Plaid sync is cursor-based, so deleted rows are not re-imported. Only a reconnect
        replays the bank's history; to keep that from bringing them back, when a bank has nothing left on or before the
        filter's "to" date its `history_from` is moved past that date and the sync ignores anything older from then on.
        GHL invoice rows are re-imported on every sync, and rows in a completed reconciliation are frozen: both are skipped.
        """
        ser = s.BulkDeleteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        targets = self._bulk_targets(d)
        deletable = targets.exclude(source="ghl").exclude(reconciliation__status="completed")
        skipped = targets.count() - deletable.count()
        deleted = deletable.count()
        item_ids = set(deletable.filter(plaid_item__isnull=False).values_list("plaid_item_id", flat=True))
        deletable.delete()
        history_from = None
        upto = d.get("filters", {}).get("to") if d.get("select_all") else None
        if upto and item_ids:
            history_from = self._cut_history(request.user, item_ids, parse_date(upto))
        audit(
            request.user,
            "transactions_bulk_deleted",
            "transactions",
            metadata={"deleted": deleted, "skipped": skipped, "history_from": history_from and history_from.isoformat()},
        )
        return Response({"deleted": deleted, "skipped": skipped, "history_from": history_from})

    @staticmethod
    def _cut_history(user, item_ids, upto):
        """Banks with nothing left on or before `upto` will ignore anything that old from now on. Returns the new start date."""
        if not upto:
            return None
        start = upto + datetime.timedelta(days=1)
        cut = []
        for item in PlaidItem.objects.filter(user=user, pk__in=item_ids):
            leftover = Transaction.objects.filter(plaid_item=item, removed_at__isnull=True, transaction_date__lte=upto)
            # Rows frozen in a completed reconciliation were kept on purpose (a statement that straddles the cut-off);
            # the sync never touches them anyway, so they do not stop the cut-off.
            if leftover.exclude(reconciliation__status="completed").exists():
                continue  # older rows survive (outside the filter): do not hide them from a future reconnect
            if item.history_from and item.history_from >= start:
                continue
            item.history_from = start
            item.save(update_fields=["history_from"])
            cut.append(item)
        return start if cut else None

    @action(detail=False, methods=["post"], url_path="detect-transfers")
    def detect_transfers(self, request):
        """Pair money moved between the user's own accounts (e.g. card payments) and mark both sides Internal Transfer."""
        n = pair_transfers(request.user)
        audit(request.user, "transfers_detected", "transactions", metadata={"updated": n})
        return Response({"updated": n})

    @action(detail=False, methods=["post"], url_path="link-transfer")
    def link_transfer(self, request):
        """The user marks two transactions as the two sides of one transfer (when automatic matching missed them)."""
        ids = request.data.get("ids")
        if not isinstance(ids, list) or len(ids) != 2:
            raise ValidationError({"detail": "Pick exactly two transactions."})
        try:
            ids = [uuid.UUID(str(i)) for i in ids]
            n = link_pair(request.user, ids)
        except ValueError as e:
            raise ValidationError({"detail": str(e)})
        audit(request.user, "transfer_linked", "transactions", metadata={"ids": [str(i) for i in ids]})
        return Response({"updated": n})

    @action(detail=False, methods=["post"], url_path="assign-ledger")
    def assign_ledger(self, request):
        ser = s.AssignLedgerSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        lid = ser.validated_data["ledger_id"]
        if lid and not Ledger.objects.for_user(request.user).filter(pk=lid).exists():
            raise ValidationError({"ledger_id": "Ledger account not found."})
        n = self._bulk_targets(ser.validated_data).update(ledger_id=lid)
        return Response({"updated": n})

    @action(detail=False, methods=["post"])
    def recategorize(self, request):
        return Response({"updated": recategorize_all(request.user)})


# ---------------------------------------------------------------- attachments
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_TX = 10
ATTACHMENT_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "heic", "csv", "xlsx", "xls", "doc", "docx", "txt"}


def _media_location(request):
    """The GoHighLevel sub-account whose media library holds this user's receipts."""
    from apps.core.actor import current_actor
    from apps.ghl.models import GhlLocation

    qs = GhlLocation.objects.filter(user=request.user, active=True)
    actor = current_actor()
    if actor and actor.get("loc"):
        qs = qs.filter(location_id=actor["loc"])
    loc = qs.order_by("name").first()
    if not loc:
        raise ValidationError({"detail": "Attachments are stored in GoHighLevel. Open this app from a connected sub-account."})
    return loc


class TransactionAttachmentsView(APIView):
    """GET lists a transaction's files; POST (multipart, field "file") uploads one to the sub-account's media library."""

    def get(self, request, tx_id):
        tx = get_object_or_404(Transaction.objects.for_user(request.user), pk=tx_id)
        return Response(s.AttachmentSerializer(tx.attachments.all(), many=True).data)

    def post(self, request, tx_id):
        from apps.ghl.services import media

        tx = get_object_or_404(Transaction.objects.for_user(request.user), pk=tx_id)
        f = request.FILES.get("file")
        if not f:
            raise ValidationError({"file": "Choose a file to upload."})
        ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
        if ext not in ATTACHMENT_EXTENSIONS:
            raise ValidationError({"file": "Unsupported file type. Use PDF, an image, or a spreadsheet/document."})
        if f.size > MAX_ATTACHMENT_BYTES:
            raise ValidationError({"file": "Files can be at most 10 MB."})
        if tx.attachments.count() >= MAX_ATTACHMENTS_PER_TX:
            raise ValidationError({"file": f"At most {MAX_ATTACHMENTS_PER_TX} files per transaction."})
        loc = _media_location(request)
        name = f.name[:150]
        try:
            stored = media.upload(loc, name, f.read(), f.content_type or "")
        except media.MediaError as e:
            return Response({"detail": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        att = Attachment.objects.create(
            user=request.user,
            transaction=tx,
            name=name,
            content_type=(f.content_type or "")[:100],
            size=f.size,
            ghl_location_id=loc.location_id,
            ghl_file_id=stored["file_id"],
            url=stored["url"],
        )
        audit(request.user, "attachment_added", "transaction", str(tx.id), {"name": name})
        return Response(s.AttachmentSerializer(att).data, status=status.HTTP_201_CREATED)


class AttachmentView(APIView):
    def get(self, request, att_id):
        """Streams the file through the API so access stays behind the user's login."""
        from apps.ghl.services import media

        att = get_object_or_404(Attachment.objects.for_user(request.user), pk=att_id)
        try:
            res = media.fetch(att.url)
        except media.MediaError as e:
            return Response({"detail": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        resp = HttpResponse(res.content, content_type=att.content_type or res.headers.get("content-type", "application/octet-stream"))
        safe = att.name.replace('"', "")
        resp["Content-Disposition"] = f'inline; filename="{safe}"'
        return resp

    def delete(self, request, att_id):
        att = get_object_or_404(Attachment.objects.for_user(request.user), pk=att_id)
        att.delete()  # post_delete removes the file from GoHighLevel (best effort)
        audit(request.user, "attachment_deleted", "transaction", str(att.transaction_id), {"name": att.name})
        return Response(status=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------- manual entries
def _entry_fields(d):
    magnitude = abs(d["amount"])
    flags = gaap_flags(d["type"])
    signed = -magnitude if flags["is_expense"] else magnitude
    if d["type"] == "equity":
        # Equity can go either way: an opening balance can be a deficit and an owner draw lowers it, so the sign is kept.
        signed = d["amount"]
    elif d["type"] == "liability":
        signed = -magnitude if d.get("direction") == "out" else magnitude
    desc = d["description"].strip()
    return {
        "ledger_id": d.get("ledger_id"),
        "transaction_date": d["date"],
        "due_date": d.get("due_date"),
        "description": desc,
        "merchant_name": desc,
        "amount": signed,
        "status": d["status"],
        "accounting_status": d.get("accounting_status"),
        "pending": d["status"] == "pending",
        "user_category_id": d.get("category_id"),
        "transaction_direction": "inflow" if signed >= 0 else "outflow",
        **flags,
    }


class EntryViewSet(viewsets.ViewSet):
    """Manual ledger entries (transactions with source='manual')."""

    def _validate_refs(self, user, d):
        if d.get("ledger_id") and not Ledger.objects.for_user(user).filter(pk=d["ledger_id"]).exists():
            raise ValidationError({"ledger_id": "Ledger account not found."})
        if d.get("category_id"):
            _category_for_user(user, d["category_id"])

    def create(self, request):
        ser = s.EntrySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        self._validate_refs(request.user, ser.validated_data)
        tx = Transaction.objects.create(
            user=request.user,
            source="manual",
            currency="USD",
            category_source="user_rule",
            categorization_confidence="high",
            **_entry_fields(ser.validated_data),
        )
        return Response({"id": str(tx.id)}, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        tx = get_object_or_404(Transaction.objects.for_user(request.user), pk=pk, source="manual")
        ser = s.EntrySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        self._validate_refs(request.user, ser.validated_data)
        data = dict(ser.validated_data)
        if data["type"] == "liability" and "direction" not in data and tx.amount < 0:
            data["direction"] = "out"  # editing other fields must not flip a payment into a receipt
        for k, v in _entry_fields(data).items():
            setattr(tx, k, v)
        tx.save()
        return Response({"ok": True})

    def destroy(self, request, pk=None):
        tx = get_object_or_404(Transaction.objects.for_user(request.user), pk=pk, source="manual")
        tx.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# -------------------------------------------------------------------- ledgers
def _clear_incompatible_statuses(ledger):
    allowed = {
        "current_asset": ["accounts_receivable", "received"],
        "current_liability": ["accounts_payable", "paid"],
    }.get(ledger.account_class, [])
    qs = Transaction.objects.filter(ledger=ledger)
    if allowed:
        qs = qs.exclude(accounting_status__in=allowed)
    qs.update(accounting_status=None)


class LedgerViewSet(viewsets.ModelViewSet):
    serializer_class = s.LedgerSerializer
    pagination_class = None
    filter_backends: list = []

    def get_queryset(self):
        return Ledger.objects.for_user(self.request.user).prefetch_related("tags").order_by("label")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        ledger = serializer.save()
        _clear_incompatible_statuses(ledger)

    def retrieve(self, request, *args, **kwargs):
        ledger = self.get_object()
        txs = (
            Transaction.objects.for_user(request.user)
            .filter(ledger=ledger, removed_at__isnull=True)
            .select_related("user_category")
            .order_by("-transaction_date")[:500]
        )
        rules = CategoryRule.objects.for_user(request.user).filter(ledger=ledger).select_related("category", "ledger").order_by("priority")
        data = s.LedgerSerializer(ledger, context=self.get_serializer_context()).data
        return Response(
            {
                "ledger": data,
                "tags": data["tags"],
                "transactions": s.TransactionSerializer(txs, many=True).data,
                "rules": s.RuleSerializer(rules, many=True, context=self.get_serializer_context()).data,
                "transaction_count": len(txs),
            }
        )

    @action(detail=False, methods=["get"])
    def summaries(self, request):
        out: dict = {}
        rows = (
            Transaction.objects.for_user(request.user)
            .filter(ledger__isnull=False, removed_at__isnull=True)
            .values_list("ledger_id", "amount")[:10000]
        )
        for lid, amount in rows:
            e = out.setdefault(str(lid), {"count": 0, "net": 0.0})
            e["count"] += 1
            e["net"] += float(amount)
        return Response({"summaries": out})

    @action(detail=True, methods=["post"], url_path="tags")
    def add_tag(self, request, pk=None):
        ledger = self.get_object()
        name = (request.data.get("name") or "").strip()
        if not name or len(name) > 40:
            raise ValidationError({"name": "Tag name must be 1-40 characters."})
        tag = LedgerTag.objects.for_user(request.user).filter(name__iexact=name).first()
        if not tag:
            tag = LedgerTag.objects.create(user=request.user, name=name)
        ledger.tags.add(tag)
        return Response({"ok": True})

    @action(detail=True, methods=["delete"], url_path=r"tags/(?P<tag_id>[^/.]+)")
    def remove_tag(self, request, pk=None, tag_id=None):
        ledger = self.get_object()
        tag = get_object_or_404(LedgerTag.objects.for_user(request.user), pk=tag_id)
        ledger.tags.remove(tag)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # --- 1099 vendor profile
    @action(detail=True, methods=["get", "put"], url_path="vendor-profile")
    def vendor_profile(self, request, pk=None):
        ledger = self.get_object()
        if request.method == "GET":
            try:
                year = int(request.query_params.get("year", ""))
            except ValueError:
                raise ValidationError({"year": "A valid year is required."})
            if not 2000 <= year <= 2100:
                raise ValidationError({"year": "Year out of range."})
            profile = VendorTaxProfile.objects.filter(ledger=ledger).first()
            if not profile:
                return Response({"profile": None, "summary": None})
            data = s.VendorProfileSerializer(profile).data
            if not profile.is_1099_vendor:
                return Response({"profile": data, "summary": None})
            pay = v1099.payments_by_ledger(request.user, year, [ledger.id])
            return Response({"profile": data, "summary": v1099.build_row(profile, ledger.label, year, pay.get(ledger.id))})

        ser = s.VendorProfileWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        fields = {
            "is_1099_vendor": d["is_1099_vendor"],
            "form_type": d["form_type"],
            "misc_box": (d.get("misc_box") or "other_income") if d["form_type"] == "misc" else None,
            "legal_name": d.get("legal_name") or None,
            "business_name": d.get("business_name") or None,
            "tax_classification": d.get("tax_classification"),
            "is_attorney": d["is_attorney"],
            "tin_type": d.get("tin_type"),
            "address_line1": d.get("address_line1") or None,
            "address_line2": d.get("address_line2") or None,
            "city": d.get("city") or None,
            "state": d["state"].upper() if d.get("state") else None,
            "postal_code": d.get("postal_code") or None,
            "w9_received_at": d.get("w9_received_at"),
            "backup_withholding": d["backup_withholding"],
        }
        if d.get("tin"):
            fields["tin_encrypted"] = encrypt_secret(d["tin"])
            fields["tin_last4"] = d["tin"][-4:]
        VendorTaxProfile.objects.update_or_create(ledger=ledger, defaults={"user": request.user, **fields})
        return Response({"ok": True})


class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = s.ProjectSerializer
    pagination_class = None
    filter_backends: list = []

    def get_queryset(self):
        return Project.objects.for_user(self.request.user).order_by("status", "-start_date", "-created_at")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def _filtered(self, p):
        """List filters: status = open | finished | all (default all); from/to keep projects active in that window.

        A project overlaps the window unless it starts after `to` or ended before `from`. A missing start or end date is
        open-ended, so undated projects match every window.
        """
        qs = self.get_queryset()
        status_ = p.get("status")
        if status_ in ("open", "finished"):
            qs = qs.filter(status=status_)
        if p.get("to"):
            qs = qs.filter(Q(start_date__lte=p["to"]) | Q(start_date__isnull=True))
        if p.get("from"):
            qs = qs.filter(Q(end_date__gte=p["from"]) | Q(end_date__isnull=True))
        return qs

    def list(self, request, *args, **kwargs):
        projects = list(self._filtered(request.query_params))
        sums = project_svc.summaries(request.user, projects)
        data = s.ProjectSerializer(projects, many=True, context=self.get_serializer_context()).data
        return Response({"projects": [{**row, "totals": sums[str(row["id"])]} for row in data]})

    @action(detail=True, methods=["get"])
    def report(self, request, pk=None):
        project = self.get_object()
        data = s.ProjectSerializer(project, context=self.get_serializer_context()).data
        return Response({"project": data, "report": project_svc.report(request.user, project)})


class TagListView(ListAPIView):
    serializer_class = s.TagSerializer
    pagination_class = None
    filter_backends: list = []

    def get_queryset(self):
        return LedgerTag.objects.for_user(self.request.user).order_by("name")


class Export1099View(APIView):
    """CSV of every 1099 vendor for a calendar year (full TINs included for filing)."""

    def get(self, request):
        try:
            year = int(request.query_params.get("year", ""))
        except ValueError:
            raise ValidationError({"year": "A valid year is required."})
        if not 2000 <= year <= 2100:
            raise ValidationError({"year": "Year out of range."})
        qs = VendorTaxProfile.objects.for_user(request.user).filter(is_1099_vendor=True).select_related("ledger")
        if request.query_params.get("ledger_id"):
            qs = qs.filter(ledger_id=request.query_params["ledger_id"])
        profiles = list(qs)
        pay = v1099.payments_by_ledger(request.user, year, [p.ledger_id for p in profiles])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "Tax Year",
                "Ledger",
                "Form",
                "Box",
                "Recipient Legal Name",
                "Business Name",
                "TIN Type",
                "TIN",
                "Tax Classification",
                "Attorney",
                "Address 1",
                "Address 2",
                "City",
                "State",
                "ZIP",
                "Reportable Payments",
                "Excluded Card/1099-K Payments",
                "Payment Count",
                "Threshold",
                "Exempt",
                "Must File",
                "W-9 Received",
                "Backup Withholding",
                "Issues",
            ]
        )
        for p in profiles:
            r = v1099.build_row(p, p.ledger.label, year, pay.get(p.ledger_id))
            tin = ""
            if p.tin_encrypted:
                try:
                    d = decrypt_secret(p.tin_encrypted)
                    tin = f"{d[:2]}-{d[2:]}" if p.tin_type == "ein" else f"{d[:3]}-{d[3:5]}-{d[5:]}"
                except Exception:
                    tin = ""
            w.writerow(
                [
                    year,
                    r["label"],
                    r["form"],
                    r["box"],
                    p.legal_name or "",
                    p.business_name or "",
                    (p.tin_type or "").upper(),
                    tin,
                    v1099.TAX_CLASSIFICATION_LABELS.get(p.tax_classification, ""),
                    "Yes" if p.is_attorney else "No",
                    p.address_line1 or "",
                    p.address_line2 or "",
                    p.city or "",
                    p.state or "",
                    p.postal_code or "",
                    f"{r['total']:.2f}",
                    f"{r['excluded_card']:.2f}",
                    r["payment_count"],
                    r["threshold"],
                    "Yes" if r["exempt"] else "No",
                    "Yes" if r["reportable"] else "No",
                    p.w9_received_at or "",
                    "Yes" if p.backup_withholding else "No",
                    "; ".join(r["issues"]),
                ]
            )
        audit(request.user, "export_1099", "vendor_tax_profiles", metadata={"year": year})
        return Response({"csv": buf.getvalue(), "count": len(profiles)})
