from apps.core.models import AuditLog, Notification


def notify(user, title: str, message: str = "", type: str = "info") -> Notification:
    return Notification.objects.create(user=user, title=title, message=message, type=type)


def audit(user, action: str, entity: str = "", entity_id: str = "", metadata: dict | None = None) -> AuditLog:
    return AuditLog.objects.create(user=user, action=action, entity=entity or None, entity_id=entity_id or None, metadata=metadata or {})
