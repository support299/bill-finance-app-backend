from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("categories", views.CategoryViewSet, basename="category")
router.register("rules", views.RuleViewSet, basename="rule")
router.register("transactions", views.TransactionViewSet, basename="transaction")
router.register("entries", views.EntryViewSet, basename="entry")
router.register("ledgers", views.LedgerViewSet, basename="ledger")
router.register("projects", views.ProjectViewSet, basename="project")

urlpatterns = [
    path("transactions/<uuid:tx_id>/attachments/", views.TransactionAttachmentsView.as_view()),
    path("attachments/<uuid:att_id>/", views.AttachmentView.as_view()),
    path("tags/", views.TagListView.as_view()),
    path("1099/export/", views.Export1099View.as_view()),
    path("", include(router.urls)),
]
