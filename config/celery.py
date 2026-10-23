import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "refresh-ghl-tokens-hourly": {"task": "apps.ghl.tasks.refresh_expiring_tokens_task", "schedule": 3600.0},
    "sync-ghl-locations-daily": {"task": "apps.ghl.tasks.sync_installed_locations_task", "schedule": 86400.0},
}
