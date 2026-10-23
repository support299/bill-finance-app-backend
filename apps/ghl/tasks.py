from celery import shared_task

from .services.locations import ensure_location_token, sync_installed_locations
from .services.oauth import refresh_expiring_tokens


@shared_task
def refresh_expiring_tokens_task() -> dict:
    return refresh_expiring_tokens()


@shared_task
def sync_installed_locations_task() -> dict:
    return sync_installed_locations()


@shared_task
def mint_location_token_task(location_id: str) -> bool:
    return ensure_location_token(location_id)


@shared_task
def import_location_invoices_task(location_id: str, user_id: str) -> dict:
    """First invoice import after a location is linked to a workspace."""
    from django.contrib.auth import get_user_model

    from .services.invoices import sync_location_invoices

    user = get_user_model().objects.get(pk=user_id)
    return sync_location_invoices(location_id, user)
