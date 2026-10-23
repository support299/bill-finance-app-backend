from django.urls import include, path

urlpatterns = [
    path("", include("apps.accounts.urls")),
    path("", include("apps.core.urls")),
    path("", include("apps.banking.urls")),
    path("", include("apps.ledger.urls")),
    path("", include("apps.reporting.urls")),
    path("", include("apps.ghl.urls")),
]
