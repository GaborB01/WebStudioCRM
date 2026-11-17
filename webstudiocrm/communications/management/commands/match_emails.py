"""Re-run email matching, e.g. after adding a contact or a client domain.

By default only the messages that still need someone - unmatched, or
flagged for review - are re-matched. Manual assignments are never touched.

    python manage.py match_emails            # unmatched + needs review
    python manage.py match_emails --all      # every automatically matched email too
    python manage.py match_emails --dry-run  # report what would change
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from communications.choices import MatchMethod
from communications.models import EmailMessage
from communications.services.matching import MatchInput, apply_match, match


class Command(BaseCommand):
    help = "Re-run client/project matching on logged emails."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Also re-match emails that are already matched and confirmed.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Show the outcome without saving it."
        )

    def handle(self, *args, **options):
        messages = EmailMessage.objects.exclude(match_method=MatchMethod.MANUAL)
        if not options["all"]:
            messages = messages.filter(Q(client__isnull=True) | Q(needs_review=True))

        outcomes = Counter()
        changed = 0
        for message in messages.order_by("sent_at", "pk").iterator():
            result = match(MatchInput.from_message(message))
            before = (message.client_id, message.project_id, message.match_method,
                      message.needs_review)
            if not options["dry_run"]:
                with transaction.atomic():
                    apply_match(message, result)
                after = (message.client_id, message.project_id, message.match_method,
                         message.needs_review)
            else:
                after = (result.client.pk if result.client else None,
                         result.project.pk if result.project else None,
                         result.method, result.needs_review)
            outcomes[MatchMethod(result.method).label] += 1
            if before != after:
                changed += 1
                self.stdout.write(f"  {message.subject or '(no subject)'}: {result.note}")

        verb = "would change" if options["dry_run"] else "changed"
        summary = ", ".join(f"{n} {label.lower()}" for label, n in sorted(outcomes.items()))
        self.stdout.write(f"{sum(outcomes.values())} email(s) checked, {changed} {verb}. "
                          f"{summary}".strip())
