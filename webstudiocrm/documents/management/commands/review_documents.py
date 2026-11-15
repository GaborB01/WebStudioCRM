"""Daily document job: moves documents through the parts of their life that
are driven by the calendar rather than by a person.

    python manage.py review_documents --settings=webstudiocrm.settings.prod

Four passes, in the order the dates apply:

1. activate  - a signed document whose start date has arrived is now in force
2. renew     - an auto-renewing document past its expiry rolls to a new term
3. expire    - any other document past its expiry is closed as expired
4. remind    - documents entering their notice window get a renewal task

Safe to run more than once a day: every pass only touches documents that are
not already in the state it would put them in.
"""

from django.core.management.base import BaseCommand

from documents.services.workflow import DocumentService


class Command(BaseCommand):
    help = "Activate, renew, expire and raise reminders for documents. Run daily (cron)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet-if-idle",
            action="store_true",
            help="Print nothing when there was nothing to do (tidier in cron mail).",
        )

    def handle(self, *args, **options):
        activated = DocumentService.activate_due()
        expired, renewed = DocumentService.expire_overdue()
        reminders = DocumentService.create_renewal_reminders()

        if options["quiet_if_idle"] and not any((activated, expired, renewed, reminders)):
            return

        for count, message in (
            (activated, "document(s) became active"),
            (renewed, "document(s) renewed for a new term"),
            (expired, "document(s) marked as expired"),
            (reminders, "renewal task(s) created"),
        ):
            style = self.style.SUCCESS if count else self.style.NOTICE
            self.stdout.write(style(f"{count} {message}."))
