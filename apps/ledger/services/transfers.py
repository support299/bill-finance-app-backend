"""Pair the two sides of a money movement between the user's own accounts and mark both as Internal Transfer.

A credit card payment from the operating account shows up twice: money out of checking, money into the card. Left alone,
the outflow is categorized as an expense and the card's own charges are expenses too, so the payment is counted twice.
Pairing finds the matching inflow (same amount, other account, within a few days) and marks both rows as transfers,
which keeps them out of the P&L. A category the user picked by hand (or set through a rule) is never overridden.
"""

import datetime
import logging
from collections import defaultdict

from django.utils import timezone

from apps.ledger.models import Category, Transaction

from .gaap import gaap_flags

logger = logging.getLogger(__name__)

MATCH_WINDOW_DAYS = 3
SOURCE = "transfer_match"  # category_source stamped on paired rows (the categorizer never produces this value)
USER_SOURCES = ("user_rule", "learned_rule")  # choices the user made (directly or through a rule): never overridden


def _transfer_category():
    return Category.objects.filter(user__isnull=True, name="Internal Transfer").first()


def _candidates(user, since: datetime.date | None):
    qs = (
        Transaction.objects.for_user(user)
        .filter(removed_at__isnull=True, pending=False, plaid_account_id__isnull=False, source__in=("plaid", "csv"))
        .exclude(status="void")
        .exclude(reconciliation__status="completed")  # frozen by a completed reconciliation
        .exclude(category_source__in=USER_SOURCES)
        .exclude(project__isnull=False)  # on a job: the user has already decided what it is
    )
    if since:
        qs = qs.filter(transaction_date__gte=since - datetime.timedelta(days=MATCH_WINDOW_DAYS))
    return qs.order_by("transaction_date", "created_at")


def pair_transfers(user, since: datetime.date | None = None) -> int:
    """Marks matching outflow/inflow pairs between different accounts as transfers. Returns the number of rows changed.

    since: only look at rows dated on/after this (minus the match window), e.g. the earliest date a sync just touched.
    Rows already marked as transfers still take part so that the other side of an existing transfer gets paired.
    """
    category = _transfer_category()
    if category is None:
        return 0
    rows = list(_candidates(user, since))
    inflows: dict = defaultdict(list)  # (amount magnitude) -> inflow rows, in date order
    for t in rows:
        if t.amount > 0:
            inflows[t.amount].append(t)
    taken: set = set()
    changed = 0
    for out in rows:
        if out.amount >= 0:
            continue
        magnitude = -out.amount
        match = None
        for inn in inflows.get(magnitude, ()):
            if inn.pk in taken or inn.plaid_account_id == out.plaid_account_id:
                continue
            if abs((inn.transaction_date - out.transaction_date).days) <= MATCH_WINDOW_DAYS:
                match = inn
                break
        if match is None:
            continue
        taken.add(match.pk)
        taken.add(out.pk)
        for t in (out, match):
            changed += _mark(t, category)
    if changed:
        logger.info("Paired %d transfer rows for user %s", changed, user.pk)
    return changed


def _mark(t: Transaction, category) -> int:
    if t.is_transfer and t.user_category_id == category.id:
        return 0
    t.user_category = category
    t.category_source = SOURCE
    t.categorization_confidence = "high"
    t.updated_at = timezone.now()
    for k, v in gaap_flags("transfer").items():
        setattr(t, k, v)
    t.save(
        update_fields=[
            "user_category",
            "category_source",
            "categorization_confidence",
            "gaap_type",
            "is_income",
            "is_expense",
            "is_transfer",
            "updated_at",
        ]
    )
    return 1


def link_pair(user, ids) -> int:
    """Marks two transactions picked by the user as the two sides of one transfer. Raises ValueError with a plain message if they can't be.

    The pair must be one outflow and one inflow on different accounts. Rows frozen by a completed reconciliation are refused.
    The choice is stored as the user's own, so automatic matching and re-categorizing never undo it.
    """
    category = _transfer_category()
    if category is None:
        raise ValueError("The Internal Transfer category is missing.")
    rows = list(Transaction.objects.for_user(user).filter(id__in=ids, removed_at__isnull=True).select_related("reconciliation"))
    if len(rows) != 2 or len(set(ids)) != 2:
        raise ValueError("Pick exactly two transactions.")
    a, b = rows
    if not ((a.amount < 0 < b.amount) or (b.amount < 0 < a.amount)):
        raise ValueError("A transfer is one payment out and one payment in. Pick one of each.")
    if not a.plaid_account_id or not b.plaid_account_id or a.plaid_account_id == b.plaid_account_id:
        raise ValueError("The two transactions must be on two different bank accounts.")
    if any(t.reconciliation_id and t.reconciliation.status == "completed" for t in rows):
        raise ValueError("One of these is part of a completed reconciliation and can't be changed.")
    for t in rows:
        t.user_category = category
        t.category_source = "user_rule"  # the user's own choice: never overridden
        t.categorization_confidence = "high"
        t.updated_at = timezone.now()
        for k, v in gaap_flags("transfer").items():
            setattr(t, k, v)
        t.save(
            update_fields=[
                "user_category",
                "category_source",
                "categorization_confidence",
                "gaap_type",
                "is_income",
                "is_expense",
                "is_transfer",
                "updated_at",
            ]
        )
    return 2
