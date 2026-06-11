from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete all Leads and ActionLogs. Keeps Campaigns and LinkedInProfiles."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")

    def handle(self, *args, **options):
        from crm.models import Lead
        from linkedin.models import ActionLog

        counts = {
            "Leads": Lead.objects.count(),
            "ActionLogs": ActionLog.objects.count(),
        }
        self.stdout.write("Will delete:")
        for name, count in counts.items():
            self.stdout.write(f"  {name}: {count}")

        if not options["yes"]:
            confirm = input("\nProceed? [y/N] ")
            if confirm.lower() != "y":
                self.stdout.write("Aborted.")
                return

        ActionLog.objects.all().delete()
        Lead.objects.all().delete()
        self.stdout.write(self.style.SUCCESS("Reset complete."))
