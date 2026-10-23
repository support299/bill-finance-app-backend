from django.db import models

from apps.core.models import OwnedModel


class SavedReport(OwnedModel):
    name = models.TextField()
    report_type = models.TextField()
    date_from = models.DateField(blank=True, null=True)
    date_to = models.DateField(blank=True, null=True)
    configuration_json = models.JSONField(default=dict)
