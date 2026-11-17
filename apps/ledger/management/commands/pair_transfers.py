"""Pairs the two sides of transfers between a user's own accounts, including credit card payments. Dry run unless --apply is given."""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.ledger.services.transfers import pair_transfers


class Command(BaseCommand):
    help = "Report (default) or apply transfer pairing for a user's transactions. Safe to repeat."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="User id or email. Default: every user.")
        parser.add_argument("--apply", action="store_true", help="Save the changes. Without this nothing is changed.")

    def handle(self, *args, user=None, apply=False, **opts):
        users = User.objects.all()
        if user:
            users = users.filter(email=user.lower()) if "@" in user else users.filter(pk=user)
            if not users.exists():
                raise CommandError(f"No user {user}")
        self.stdout.write("APPLYING" if apply else "DRY RUN (nothing is written)")
        for u in users.iterator():
            n = pair_transfers(u, dry_run=not apply)
            if n:
                self.stdout.write(f"{str(u.pk)[:8]} rows {'changed' if apply else 'that would change'}: {n}")
