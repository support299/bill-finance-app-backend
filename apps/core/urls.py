from django.urls import path

from . import views

urlpatterns = [
    path("notifications/", views.NotificationListView.as_view()),
    path("notifications/mark-read/", views.NotificationMarkReadView.as_view()),
]
