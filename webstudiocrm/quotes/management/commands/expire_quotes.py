from django.core.management.base import BaseCommand

from quotes.services.workflow import QuoteService


class Command(BaseCommand):
    help = "Mark sent quotes whose validity date has passed as expired. Run daily (cron)."

    def handle(self, *args, **options):
        count = QuoteService.expire_overdue()
        self.stdout.write(self.style.SUCCESS(f"{count} quote(s) marked as expired."))
