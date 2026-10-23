from django.core.management.base import BaseCommand

from apps.ghl.models import GhlLocation
from apps.ghl.services.locations import refresh_location_details, sync_installed_locations


class Command(BaseCommand):
    help = "Import every subaccount the agency installed the app on, then refresh the details of locations that have no name."

    def handle(self, *args, **options):
        result = sync_installed_locations()
        self.stdout.write(f"Imported {result['locations']} installed location(s).")
        for loc in GhlLocation.objects.filter(name__isnull=True):
            refresh_location_details(loc)
        self.stdout.write(self.style.SUCCESS("Done."))
