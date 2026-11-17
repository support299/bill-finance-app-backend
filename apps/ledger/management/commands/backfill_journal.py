"""Builds double-entry journal entries from existing transactions. Dry run unless --apply is given."""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.ledger.services.journal import backfill, opening_balances


class Command(BaseCommand):
    help = "Report (default) or create journal entries for a user's transactions. Safe to repeat."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="User id or email. Default: every user.")
        parser.add_argument(
            "--opening-balances", action="store_true", help="Also set each bank account's opening balance from the bank's balance."
        )
        parser.add_argument("--apply", action="store_true", help="Write the entries. Without this nothing is changed.")

    def handle(self, *args, user=None, apply=False, **opts):
        users = User.objects.all()
        if user:
            users = users.filter(email=user.lower()) if "@" in user else users.filter(pk=user)
            if not users.exists():
                raise CommandError(f"No user {user}")
        self.stdout.write(f"{'APPLYING' if apply else 'DRY RUN (nothing is written)'}")
        for u in users.iterator():
            r = backfill(u, apply=apply)
            if not r["mapped"] and not r["skipped"]:
                continue
            self.stdout.write(
                f"{str(u.pk)[:8]} mapped={r['mapped']} skipped={r['skipped']} debit={r['total_debit']} credit={r['total_credit']} "
                f"balanced={r['balanced']} transfers_in_transit={r['transfers_in_transit']} receivable_difference={r['receivable']['difference']}"
            )
            if opts["opening_balances"]:
                for o in opening_balances(u, apply=apply):
                    if "skipped" in o:
                        self.stdout.write(f"  {o['account']}: skipped ({o['skipped']})")
                    else:
                        self.stdout.write(
                            f"  {o['account']}: bank says {o['bank_balance']}, transactions add up to {o['from_transactions']}, "
                            f"opening balance {o['opening']} on {o['date']}"
                        )
