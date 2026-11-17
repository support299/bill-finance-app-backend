"""Side-by-side check: current reports vs reports built from the journal."""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.reporting.services.ledger_reports import compare_with_current, reconciliation_check


class Command(BaseCommand):
    help = "Prints each headline number from the current reports next to the journal's. Read only."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="User id or email.")
        parser.add_argument("--from", dest="date_from", default=f"{date.today().year}-01-01")
        parser.add_argument("--to", dest="date_to", default=date.today().isoformat())

    def handle(self, *args, user, date_from, date_to, **opts):
        found = User.objects.filter(email=user.lower()) if "@" in user else User.objects.filter(pk=user)
        if not found.exists():
            raise CommandError(f"No user {user}")
        r = compare_with_current(found.first(), date_from=date.fromisoformat(date_from), date_to=date.fromisoformat(date_to))
        self.stdout.write(f"{'measure':34}{'current':>16}{'entries':>16}{'difference':>14}")
        for row in r["rows"]:
            self.stdout.write(f"{row['measure']:34}{row['current']:>16,.2f}{row['entries']:>16,.2f}{row['difference']:>14,.2f}")
        self.stdout.write("MATCH" if r["matches"] else "DO NOT MATCH")
        rc = reconciliation_check(found.first())
        self.stdout.write(
            f"\nReconciliations: {len(rc['rows'])} checked, " + ("all agree with the journal" if rc["matches"] else "DIFFERENCES")
        )
        for row in rc["rows"]:
            if row["difference"]:
                self.stdout.write(
                    f"  {row['account']} {row['statement_date']}: transactions {row['cleared_from_transactions']:,.2f}, journal {row['cleared_from_journal']:,.2f}"
                )
