import json

from django.core.management.base import BaseCommand, CommandError

from apps.ghl.services.lovable_import import ImportError_, import_location


class Command(BaseCommand):
    help = "Import one location's data exported from the old app (tools/export_lovable_location.py). Re-running never duplicates."

    def add_arguments(self, parser):
        parser.add_argument("file", help="JSON file made by tools/export_lovable_location.py")
        parser.add_argument("--location", required=True, help="GoHighLevel location id the file belongs to")
        parser.add_argument("--dry-run", action="store_true", help="Do everything, report the counts, then roll back")

    def handle(self, *args, file, location, dry_run, **options):
        with open(file) as fh:
            data = json.load(fh)
        try:
            stats = import_location(data, location, dry_run=dry_run)
        except ImportError_ as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(("DRY RUN (nothing saved)" if dry_run else "IMPORTED") + f" into {stats.pop('workspace_user')}:")
        for key, value in sorted(stats.items()):
            self.stdout.write(f"  {key}: {value}")
