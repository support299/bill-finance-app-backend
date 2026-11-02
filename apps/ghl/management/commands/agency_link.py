import secrets

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Make or show the agency client-list link. Generates GHL_AGENCY_TOKEN for you when none is set."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="GoHighLevel company (agency) id")

    def handle(self, *args, company=None, **options):
        token = settings.GHL_AGENCY_TOKEN
        if not token:
            token = secrets.token_urlsafe(32)
            self.stdout.write("No GHL_AGENCY_TOKEN is set. Add this line to backend/.env and restart the server:\n")
            self.stdout.write(f"  GHL_AGENCY_TOKEN={token}\n")
        base = f"{settings.FRONTEND_URL.rstrip('/')}/agency"
        self.stdout.write(
            f"Agency link (keep it private - anyone with it can open the client list):\n  {base}?company={company or '<COMPANY_ID>'}&token={token}"
        )
