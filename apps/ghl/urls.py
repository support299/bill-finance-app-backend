from django.urls import path

from . import views, views_access

urlpatterns = [
    path("workspace/install/", views.InstallView.as_view()),
    path("workspace/oauth/callback/", views.OAuthCallbackView.as_view()),
    path("workspace/session/", views.SessionView.as_view()),
    path("workspace/sso/", views_access.SsoSessionView.as_view()),
    path("workspace/me/", views_access.MeView.as_view()),
    path("workspace/members/", views_access.MembersView.as_view()),
    path("workspace/members/<str:location_id>/<str:ghl_user_id>/", views_access.MemberDetailView.as_view()),
    path("workspace/locations/", views_access.SwitchLocationsView.as_view()),
    path("workspace/activity/", views_access.ActivityView.as_view()),
    path("workspace/links/", views.LinksView.as_view()),
    path("workspace/links/<str:location_id>/", views.LinkDetailView.as_view()),
    path("workspace/link/options/", views.LinkOptionsView.as_view()),
    path("workspace/link/confirm/", views.LinkConfirmView.as_view()),
    path("workspace/refresh-tokens/", views.RefreshTokensView.as_view()),
    path("workspace/invoices/sync/", views.SyncInvoicesView.as_view()),
    path("workspace/ledgers/<uuid:ledger_id>/invoices/", views.LedgerInvoicesView.as_view()),
    path("workspace/webhook/", views.ghl_webhook),
    path("agency/session/", views.AgencySessionView.as_view()),
    path("agency/clients/", views.AgencyClientsView.as_view()),
    path("agency/clients/<str:location_id>/open/", views.AgencyOpenClientView.as_view()),
]
