import csv
import io

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.crypto import decrypt_secret, encrypt_secret
from apps.core.services import audit

from . import serializers as s
from .models import CategoryRule, Ledger, LedgerTag, Transaction, VendorTaxProfile
from .services import vendor1099 as v1099
from .services.gaap import accounting_statuses_for_type, gaap_flags, row_gaap_type
from .services.transactions import (
    apply_filters,
    apply_rule_to_existing,
    apply_sort,
    recategorize_all,
    visible_categories,
)


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

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, is_system=False)


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

    def perform_update(self, serializer):
        serializer.validated_data.pop("apply_to_existing", None)
        serializer.save()


# --------------------------------------------------------------- transactions
class TransactionViewSet(viewsets.GenericViewSet):
    serializer_class = s.TransactionSerializer
    filter_backends: list = []
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        return Transaction.objects.for_user(self.request.user).select_related("user_category")

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

    @action(detail=False, methods=["post"], url_path="bulk-categorize")
    def bulk_categorize(self, request):
        ser = s.BulkCategorizeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cat = _category_for_user(request.user, ser.validated_data["category_id"])
        n = (
            self.get_queryset()
            .filter(id__in=ser.validated_data["ids"])
            .update(user_category=cat, category_source="user_rule", categorization_confidence="high", **gaap_flags(cat.type))
        )
        return Response({"updated": n})

    @action(detail=False, methods=["post"], url_path="assign-ledger")
    def assign_ledger(self, request):
        ser = s.AssignLedgerSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        lid = ser.validated_data["ledger_id"]
        if lid and not Ledger.objects.for_user(request.user).filter(pk=lid).exists():
            raise ValidationError({"ledger_id": "Ledger account not found."})
        n = self.get_queryset().filter(id__in=ser.validated_data["ids"]).update(ledger_id=lid)
        return Response({"updated": n})

    @action(detail=False, methods=["post"])
    def recategorize(self, request):
        return Response({"updated": recategorize_all(request.user)})


# ------------------------------------------------------------- manual entries
def _entry_fields(d):
    magnitude = abs(d["amount"])
    flags = gaap_flags(d["type"])
    signed = -magnitude if flags["is_expense"] else magnitude
    desc = d["description"].strip()
    return {
        "ledger_id": d["ledger_id"],
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
        if not Ledger.objects.for_user(user).filter(pk=d["ledger_id"]).exists():
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
        for k, v in _entry_fields(ser.validated_data).items():
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
