from django.urls import path

from . import views

urlpatterns = [
    path("workspace/install/", views.InstallView.as_view()),
    path("workspace/oauth/callback/", views.OAuthCallbackView.as_view()),
    path("workspace/session/", views.SessionView.as_view()),
    path("workspace/links/", views.LinksView.as_view()),
    path("workspace/links/<str:location_id>/", views.LinkDetailView.as_view()),
    path("workspace/link/options/", views.LinkOptionsView.as_view()),
    path("workspace/link/confirm/", views.LinkConfirmView.as_view()),
    path("workspace/refresh-tokens/", views.RefreshTokensView.as_view()),
    path("workspace/invoices/sync/", views.SyncInvoicesView.as_view()),
    path("workspace/ledgers/<uuid:ledger_id>/invoices/", views.LedgerInvoicesView.as_view()),
    path("workspace/webhook/", views.ghl_webhook),
]
