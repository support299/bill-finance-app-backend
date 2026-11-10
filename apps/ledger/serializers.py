from rest_framework import serializers

from .models import (
    ACCOUNT_CLASSES,
    ACCOUNTING_STATUSES,
    GAAP_TYPES,
    PROJECT_ROLES,
    Attachment,
    Category,
    CategoryRule,
    Ledger,
    LedgerTag,
    Project,
    Transaction,
    VendorTaxProfile,
)
from .services.gaap import accounting_statuses_for_type

TX_STATUSES = ["pending", "posted", "paid", "void"]


class CategoryLiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "type", "color", "icon"]


class CategorySerializer(serializers.ModelSerializer):
    user_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Category
        fields = [
            "id",
            "user_id",
            "name",
            "type",
            "parent_category",
            "sort_order",
            "icon",
            "color",
            "is_system",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "user_id", "is_system", "created_at", "updated_at"]
        extra_kwargs = {"name": {"max_length": 60}}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        mine = self.context.get("positions")
        if mine is not None and instance.pk in mine:
            data["sort_order"] = mine[instance.pk]  # this user's own order, over the default
        return data

    def validate_parent_category(self, parent):
        if parent is None:
            return None
        user = self.context["request"].user
        if parent.user_id not in (None, user.id):
            raise serializers.ValidationError("Parent category not found.")
        if parent.parent_category_id:
            raise serializers.ValidationError("Sub-categories can only go one level deep.")
        if self.instance:
            if parent.pk == self.instance.pk:
                raise serializers.ValidationError("A category cannot be its own parent.")
            if self.instance.children.exists():
                raise serializers.ValidationError("This category has sub-categories, so it cannot become one itself.")
        return parent

    def validate(self, data):
        parent = data.get("parent_category", getattr(self.instance, "parent_category", None))
        type_ = data.get("type", getattr(self.instance, "type", None))
        if parent and type_ and parent.type != type_:
            raise serializers.ValidationError({"type": f"A sub-category must have the same type as its parent ({parent.type})."})
        return data


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerTag
        fields = ["id", "name"]


class LedgerSerializer(serializers.ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)
    account_class = serializers.ChoiceField(choices=ACCOUNT_CLASSES)
    label = serializers.CharField(max_length=120)
    contact_name = serializers.CharField(max_length=160, required=False, allow_blank=True, allow_null=True)
    contact_email = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True)
    contact_phone = serializers.CharField(max_length=60, required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = Ledger
        fields = [
            "id",
            "label",
            "account_class",
            "contact_name",
            "contact_email",
            "contact_phone",
            "notes",
            "ghl_contact_id",
            "tags",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "ghl_contact_id", "created_at", "updated_at"]

    def validate_label(self, value):
        value = value.strip()
        qs = Ledger.objects.for_user(self.context["request"].user).filter(label=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Labels must be unique.")
        return value

    def to_internal_value(self, data):
        out = super().to_internal_value(data)
        for f in ("contact_name", "contact_email", "contact_phone", "notes"):
            if f in out:
                out[f] = out[f] or None
        return out


class TransactionSerializer(serializers.ModelSerializer):
    """Row shape the UI already consumes: *_id columns plus an embedded `categories` object."""

    user_category_id = serializers.UUIDField(read_only=True)
    ledger_id = serializers.UUIDField(read_only=True)
    plaid_item_id = serializers.UUIDField(read_only=True)
    user_id = serializers.UUIDField(read_only=True)
    categories = CategoryLiteSerializer(source="user_category", read_only=True)
    reconciled = serializers.SerializerMethodField()  # part of a completed bank reconciliation (locked)
    attachment_count = serializers.IntegerField(read_only=True, default=0)  # annotated by the list view

    class Meta:
        model = Transaction
        exclude = ["user", "user_category", "ledger", "plaid_item", "reconciliation"]

    def get_reconciled(self, obj):
        return bool(obj.reconciliation_id and obj.reconciliation.status == "completed")


class RulePreviewSerializer(serializers.Serializer):
    keyword = serializers.CharField(min_length=2, max_length=120)
    match_type = serializers.ChoiceField(choices=["contains", "starts_with", "exact", "regex"], default="contains")
    category_id = serializers.UUIDField(required=False, allow_null=True)


class RuleSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(source="category", queryset=Category.objects.none(), required=False, allow_null=True)
    ledger_id = serializers.PrimaryKeyRelatedField(source="ledger", queryset=Ledger.objects.none(), required=False, allow_null=True)
    keyword = serializers.CharField(min_length=2, max_length=120)
    priority = serializers.IntegerField(min_value=1, max_value=1000, default=100)
    categories = CategoryLiteSerializer(source="category", read_only=True)
    ledgers = serializers.SerializerMethodField()
    apply_to_existing = serializers.BooleanField(write_only=True, default=True)

    class Meta:
        model = CategoryRule
        fields = [
            "id",
            "keyword",
            "match_type",
            "category_id",
            "ledger_id",
            "priority",
            "active",
            "source",
            "categories",
            "ledgers",
            "apply_to_existing",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_ledgers(self, obj):
        return {"id": str(obj.ledger_id), "label": obj.ledger.label} if obj.ledger_id else None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            self.fields["category_id"].queryset = category_queryset(request.user)
            self.fields["ledger_id"].queryset = Ledger.objects.for_user(request.user)

    def validate(self, data):
        creating = self.instance is None
        if creating and not (data.get("category") or data.get("ledger")):
            raise serializers.ValidationError("Pick a category, a ledger account, or both.")
        if data.get("source") and data["source"] not in ("user", "learned"):
            raise serializers.ValidationError({"source": "Must be 'user' or 'learned'."})
        return data


def category_queryset(user):
    """System categories plus the user's own (replaces the Supabase RLS read policy)."""
    from django.db.models import Q

    return Category.objects.filter(Q(user__isnull=True) | Q(user=user))


class TransactionUpdateSerializer(serializers.Serializer):
    """Unified patch for /transactions/{id}/ (category, visibility, classification, ledger status)."""

    category_id = serializers.UUIDField(required=False, allow_null=True)
    is_hidden = serializers.BooleanField(required=False)
    classification = serializers.ChoiceField(choices=GAAP_TYPES, required=False)
    status = serializers.ChoiceField(choices=TX_STATUSES, required=False)
    type = serializers.ChoiceField(choices=GAAP_TYPES, required=False)
    ledger_id = serializers.UUIDField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    accounting_status = serializers.ChoiceField(choices=ACCOUNTING_STATUSES, required=False, allow_null=True)
    project_id = serializers.UUIDField(required=False, allow_null=True)
    project_role = serializers.ChoiceField(choices=PROJECT_ROLES, required=False, allow_null=True)


class BulkTargetSerializer(serializers.Serializer):
    """Which transactions a bulk action touches: explicit ids, or everything matching the list filters (select_all)."""

    ids = serializers.ListField(child=serializers.UUIDField(), max_length=500, required=False)
    select_all = serializers.BooleanField(required=False, default=False)
    filters = serializers.DictField(required=False)  # same keys as the list endpoint's query params
    expected_count = serializers.IntegerField(required=False, min_value=0)  # what the user saw; guards against stale filters

    def validate(self, attrs):
        if attrs.get("select_all"):
            attrs.setdefault("filters", {})
        elif not attrs.get("ids"):
            raise serializers.ValidationError({"ids": "Select at least one transaction."})
        return attrs


class BulkCategorizeSerializer(BulkTargetSerializer):
    category_id = serializers.UUIDField()


class BulkAssignProjectSerializer(BulkTargetSerializer):
    project_id = serializers.UUIDField(allow_null=True)  # null removes the transactions from their project
    project_role = serializers.ChoiceField(choices=PROJECT_ROLES, required=False, allow_null=True)


class ProjectSerializer(serializers.ModelSerializer):
    ledger_id = serializers.PrimaryKeyRelatedField(source="ledger", queryset=Ledger.objects.none(), required=False, allow_null=True)
    name = serializers.CharField(min_length=1, max_length=120)

    class Meta:
        model = Project
        fields = ["id", "name", "ledger_id", "status", "budget", "notes", "start_date", "end_date", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "The end date must be on or after the start date."})
        return attrs

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            self.fields["ledger_id"].queryset = Ledger.objects.for_user(request.user)


class BulkDeleteSerializer(BulkTargetSerializer):
    pass


class AssignLedgerSerializer(BulkTargetSerializer):
    ledger_id = serializers.UUIDField(allow_null=True)


class EntrySerializer(serializers.Serializer):
    ledger_id = serializers.UUIDField(required=False, allow_null=True)
    date = serializers.DateField()
    due_date = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(min_length=1, max_length=300)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    type = serializers.ChoiceField(choices=GAAP_TYPES)
    status = serializers.ChoiceField(choices=TX_STATUSES, default="posted")
    accounting_status = serializers.ChoiceField(choices=ACCOUNTING_STATUSES, required=False, allow_null=True)
    category_id = serializers.UUIDField(required=False, allow_null=True)
    # Liabilities can move either way (tax collected vs. tax paid, loan drawn vs. repaid); "out" records money paid out.
    direction = serializers.ChoiceField(choices=["in", "out"], required=False)

    def validate(self, d):
        status = d.get("accounting_status")
        if status and status not in accounting_statuses_for_type(d["type"]):
            raise serializers.ValidationError({"accounting_status": "That accounting status does not match the transaction type."})
        return d


class VendorProfileSerializer(serializers.ModelSerializer):
    """Public columns only: the encrypted TIN is never returned."""

    ledger_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = VendorTaxProfile
        fields = [
            "ledger_id",
            "is_1099_vendor",
            "form_type",
            "misc_box",
            "legal_name",
            "business_name",
            "tax_classification",
            "is_attorney",
            "tin_type",
            "tin_last4",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "w9_received_at",
            "backup_withholding",
        ]
        read_only_fields = ["tin_last4"]


class VendorProfileWriteSerializer(serializers.Serializer):
    is_1099_vendor = serializers.BooleanField()
    form_type = serializers.ChoiceField(choices=["nec", "misc"])
    misc_box = serializers.ChoiceField(
        choices=["rents", "royalties", "other_income", "medical", "attorney_proceeds"], required=False, allow_null=True
    )
    legal_name = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True)
    business_name = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True)
    tax_classification = serializers.ChoiceField(
        choices=[
            "individual",
            "llc_single",
            "llc_partnership",
            "llc_c_corp",
            "llc_s_corp",
            "partnership",
            "c_corp",
            "s_corp",
            "trust_estate",
            "other",
        ],
        required=False,
        allow_null=True,
    )
    is_attorney = serializers.BooleanField(default=False)
    tin_type = serializers.ChoiceField(choices=["ssn", "ein", "itin"], required=False, allow_null=True)
    tin = serializers.CharField(required=False, write_only=True)
    address_line1 = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True)
    address_line2 = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    state = serializers.CharField(max_length=2, required=False, allow_blank=True, allow_null=True)
    postal_code = serializers.CharField(max_length=10, required=False, allow_blank=True, allow_null=True)
    w9_received_at = serializers.DateField(required=False, allow_null=True)
    backup_withholding = serializers.BooleanField(default=False)

    def validate_tin(self, value):
        digits = "".join(c for c in value if c.isdigit())
        if len(digits) != 9:
            raise serializers.ValidationError("TIN must be 9 digits")
        return digits


class AttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attachment
        fields = ["id", "name", "content_type", "size", "created_at"]
