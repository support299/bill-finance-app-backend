from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("saved-reports", views.SavedReportViewSet, basename="saved-report")

urlpatterns = [
    path("dashboard/", views.DashboardView.as_view()),
    path("reports/data/", views.ReportDataView.as_view()),
    path("reports/accounting/", views.AccountingReportsView.as_view()),
    path("", include(router.urls)),
]
