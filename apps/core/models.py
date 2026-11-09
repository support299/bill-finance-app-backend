import uuid

from django.conf import settings
from django.db import models


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimestampedModel(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class OwnedQuerySet(models.QuerySet):
    """Tenant isolation replacing Supabase RLS: every owned query must go through for_user()."""

    def for_user(self, user):
        return self.filter(user=user)


class OwnedModel(TimestampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    objects = OwnedQuerySet.as_manager()

    class Meta:
        abstract = True


class Notification(UUIDModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.TextField()
    message = models.TextField(blank=True, null=True)
    type = models.CharField(max_length=20, default="info")
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OwnedQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return self.title


class AuditLog(UUIDModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.TextField()
    entity = models.TextField(blank=True, null=True)
    entity_id = models.TextField(blank=True, null=True)
    metadata = models.JSONField(default=dict)
    # The GoHighLevel user who did it, when the request came from an embedded SSO session (the `user` is the sub-account).
    actor_id = models.TextField(blank=True, null=True)
    actor_name = models.TextField(blank=True, null=True)
    actor_email = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"], name="auditlog_user_created_idx")]

    def __str__(self):
        return self.action
