from celery import shared_task

from .services.sync import refresh_item_status, sync_item


@shared_task
def sync_item_task(item_id: str) -> dict:
    return sync_item(item_id, trigger="webhook").as_dict()


@shared_task
def refresh_item_status_task(item_id: str) -> None:
    refresh_item_status(item_id)
