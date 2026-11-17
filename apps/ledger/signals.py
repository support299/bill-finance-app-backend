"""Keeps the journal in step with every transaction that is saved one at a time (sync, manual entries, edits, GHL postings).
Bulk writes that skip save() call journal.repost / update_and_repost instead. Deleting a transaction removes its entry by cascade."""

import logging

from django.db import transaction as db_transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Transaction
from .services import journal

log = logging.getLogger(__name__)


@receiver(post_save, sender=Transaction, dispatch_uid="journal_post_transaction")
def post_to_journal(sender, instance, raw=False, **kwargs):
    if raw:  # loaddata
        return
    try:
        with db_transaction.atomic():
            journal.post_transaction(instance.user, instance, journal.Context(instance.user, preload=False), {})
    except Exception:  # noqa: BLE001 - a bookkeeping problem must never block saving the transaction
        log.exception("journal: could not post transaction %s", instance.pk)
