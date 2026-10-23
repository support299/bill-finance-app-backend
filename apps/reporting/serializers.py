from rest_framework import serializers

from .models import SavedReport


class RangeSerializer(serializers.Serializer):
    # `from` is a Python keyword, so the query param is read via source mapping.
    date_from = serializers.DateField(source="from")
    date_to = serializers.DateField(source="to")


class ReportRangeSerializer(serializers.Serializer):
    from_ = serializers.DateField()
    to = serializers.DateField()
    account_ids = serializers.ListField(child=serializers.CharField(max_length=100), required=False)
    include_transfers = serializers.BooleanField(default=False)
    granularity = serializers.ChoiceField(choices=["day", "week", "month"], default="month")


class SavedReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedReport
        fields = ["id", "name", "report_type", "date_from", "date_to", "configuration_json", "created_at"]
        read_only_fields = ["id", "created_at"]
