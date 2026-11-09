from apps.core.models import AuditLog, Notification

from ..actor import current_actor


def notify(user, title: str, message: str = "", type: str = "info") -> Notification:
    return Notification.objects.create(user=user, title=title, message=message, type=type)


def audit(user, action: str, entity: str = "", entity_id: str = "", metadata: dict | None = None) -> AuditLog:
    actor = current_actor() or {}
    return AuditLog.objects.create(
        user=user,
        action=action,
        entity=entity or None,
        entity_id=entity_id or None,
        metadata=metadata or {},
        actor_id=actor.get("uid") or None,
        actor_name=actor.get("name") or None,
        actor_email=actor.get("email") or None,
    )
