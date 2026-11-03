from django.urls import path

from . import views
from .webhooks import plaid_webhook

urlpatterns = [
    path("plaid/config/", views.PlaidConfigView.as_view()),
    path("plaid/link-token/", views.LinkTokenView.as_view()),
    path("plaid/exchange/", views.ExchangeView.as_view()),
    path("plaid/sync/", views.SyncView.as_view()),
    path("plaid/items/<uuid:item_id>/refresh/", views.RefreshView.as_view()),
    path("plaid/items/<uuid:item_id>/status/", views.ItemStatusView.as_view()),
    path("plaid/items/<uuid:item_id>/", views.ItemDetailView.as_view()),
    path("accounts/csv-import/", views.CsvImportView.as_view()),
    path("accounts/", views.AccountsView.as_view()),
    path("reconciliations/", views.ReconciliationListView.as_view()),
    path("reconciliations/<uuid:rec_id>/", views.ReconciliationDetailView.as_view()),
    path("reconciliations/<uuid:rec_id>/clear/", views.ReconciliationClearView.as_view()),
    path("reconciliations/<uuid:rec_id>/finish/", views.ReconciliationFinishView.as_view()),
    path("reconciliations/<uuid:rec_id>/reopen/", views.ReconciliationReopenView.as_view()),
    path("sync-status/", views.SyncStatusView.as_view()),
    path("webhooks/plaid/", plaid_webhook),
]
