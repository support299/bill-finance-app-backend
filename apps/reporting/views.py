from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import serializers as s
from .models import SavedReport
from .services import accounting, reports


def _parse_range(params):
    """Reads from/to/account_ids/include_transfers/granularity from query params."""
    ser = s.ReportRangeSerializer(
        data={
            "from_": params.get("from"),
            "to": params.get("to"),
            "account_ids": params.getlist("account_ids"),
            "include_transfers": params.get("include_transfers", "false"),
            "granularity": params.get("granularity", "month"),
        }
    )
    ser.is_valid(raise_exception=True)
    d = ser.validated_data
    if d["from_"] > d["to"]:
        raise ValidationError({"from": "Start date must be on or before the end date."})
    return d


class ReportDataView(APIView):
    def get(self, request):
        d = _parse_range(request.query_params)
        return Response(
            reports.report_data(
                request.user,
                date_from=d["from_"],
                date_to=d["to"],
                account_ids=d.get("account_ids"),
                include_transfers=d["include_transfers"],
                granularity=d["granularity"],
            )
        )


class DashboardView(APIView):
    def get(self, request):
        d = _parse_range(request.query_params)
        return Response(reports.dashboard_data(request.user, d.get("account_ids")))


class AccountingReportsView(APIView):
    def get(self, request):
        d = _parse_range(request.query_params)
        return Response(
            accounting.accounting_reports(
                request.user, date_from=d["from_"], date_to=d["to"], project_id=request.query_params.get("project_id") or None
            )
        )


class SavedReportViewSet(viewsets.ModelViewSet):
    serializer_class = s.SavedReportSerializer
    filter_backends: list = []

    def get_queryset(self):
        return SavedReport.objects.for_user(self.request.user).order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
